"""Read-only GoDaddy v3 batch pricing, reusing the tested probe and local token loader."""
from decimal import Decimal
from time import monotonic, sleep

import requests

from domain_pricing import PricingError, now, valid_domain
from godaddy_pricing_test import ENDPOINT, ProbeError, local_token, summarize


def pricing_record(domain, data):
    record = {'domain': domain, 'provider': 'GoDaddy', 'source': 'GoDaddy v3 availability API',
              'checked_at': now(), 'availability': 'Unknown', 'purchase_available': False,
              'premium': None, 'registration_price': None, 'premium_purchase_fee': None,
              'acquisition_price': None, 'acquisition_lower_bound': None,
              'annual_renewal_price': None, 'currency': None, 'price_kind': 'registration',
              'verified': False, 'total_verified': False,
              'charges_status': 'ICANN fee and taxes not returned; final total unverified', 'message': ''}
    try:
        parsed = summarize(domain, data)
    except ProbeError as exc:
        record['message'] = str(exc)
        return record
    registration = parsed['registration_price']
    renewal = parsed['annual_renewal_price']
    fees = parsed['premium_purchase_fees']
    currency = parsed['currency']
    record.update(
        availability=('Registered' if parsed['resale_listing'] else 'Available') if parsed['available'] else 'Unavailable',
        purchase_available=parsed['available'], premium=parsed['premium'], currency=currency,
        inventory=parsed['inventory'], definitive=parsed['definitive'],
        price_kind='resale' if parsed['resale_listing'] else 'registration',
        registration_price=registration['amount'] if registration else None,
        annual_renewal_price=renewal['amount'] if renewal and renewal['currency'] == currency else None,
        message=parsed['notes'])
    if registration and registration['amount'] is not None:
        fee_total = sum((Decimal(f['amount']) for f in fees if f['currency'] == currency and f['amount'] is not None),
                        Decimal(0))
        record['acquisition_lower_bound'] = str(Decimal(registration['amount']) + fee_total)
        if all(f['currency'] == currency and f['amount'] is not None for f in fees):
            if fees or parsed['inventory'] == 'REGISTRY':
                record['premium_purchase_fee'] = str(fee_total)
        record['acquisition_price'] = parsed['indicative_acquisition_excluding_tax_and_icann']
        record['verified'] = record['acquisition_price'] is not None
    if not parsed['available']:
        # Unavailable names must never inherit the registration tariff or a resale asking price.
        record.update(registration_price=None, premium_purchase_fee=None, acquisition_price=None,
                      acquisition_lower_bound=None, annual_renewal_price=None, verified=False)
    elif not record['verified']:
        record['message'] += ' Acquisition quote incomplete: missing annual price, inventory or premium fees.'
    return record


class GoDaddyPricing:
    batch_size = 25
    requires_tariff = False

    def __init__(self):
        try:
            self._token = local_token()
        except ProbeError as exc:
            raise PricingError(str(exc)) from None
        if not self._token or any(c.isspace() for c in self._token):
            raise PricingError('GoDaddy needs a valid GODADDY_PAT environment variable or local .env.godaddy. '
                               'Enter it locally, never in chat.')
        self._last_request = None
        self._remaining = None

    def check_batch(self, domains, standard=None):
        if (not domains or len(domains) > self.batch_size or len(set(domains)) != len(domains)
                or any(not valid_domain(d) for d in domains)):
            raise PricingError('GoDaddy batch pricing requires 1 to 25 distinct valid .com domains.')
        if self._remaining == 0:
            raise PricingError('GoDaddy quota exhausted. Completed batches remain saved; wait before checking again.')
        if self._last_request is not None:
            sleep(max(0, 2.5 - (monotonic() - self._last_request)))
        self._last_request = monotonic()
        try:
            with requests.Session() as session:
                response = session.post(
                    ENDPOINT, json={'domains': domains, 'optimizeFor': 'ACCURACY'},
                    headers={'Authorization': 'Bearer ' + self._token, 'Accept': 'application/json'},
                    timeout=(4, 12), allow_redirects=False)
        except requests.Timeout:
            raise PricingError('GoDaddy pricing timed out. Stopped with no automatic retry.') from None
        except requests.RequestException:
            raise PricingError('GoDaddy pricing network request failed. Stopped with no automatic retry.') from None
        if response.status_code != 200:
            messages = {
                401: 'PAT invalid or expired; update it locally.',
                402: 'Payment/access required. Stopped; no paid operation attempted.',
                403: 'Access denied; verify domains.domain:read / Domain Search permission and account eligibility.',
                429: 'Rate limit reached; wait before a separate manual check.',
            }
            wait = response.headers.get('Retry-After', response.headers.get('RateLimit-Reset', ''))
            wait_note = f' Wait {wait} seconds.' if isinstance(wait, str) and wait.isdigit() else ''
            raise PricingError(f'GoDaddy HTTP {response.status_code}: ' +
                               messages.get(response.status_code, 'Provider rejected the lookup.') +
                               wait_note + ' No retries or further requests sent.')
        remaining = response.headers.get('RateLimit-Remaining', '')
        if isinstance(remaining, str) and remaining.isdigit():
            self._remaining = int(remaining)
        try:
            data = response.json()
        except ValueError:
            raise PricingError('GoDaddy returned invalid JSON; stopped without displaying the raw response.') from None
        items = data.get('items') if isinstance(data, dict) else None
        if not isinstance(items, list) or len(items) != len(domains):
            raise PricingError('GoDaddy returned an incomplete batch. This batch was not saved; no retry sent.')
        results = {}
        for item in items:
            domain = item.get('domain') if isinstance(item, dict) else None
            if not isinstance(domain, str) or domain.lower() not in domains or domain.lower() in results:
                raise PricingError('GoDaddy returned mismatched or duplicate domains. This batch was not saved.')
            domain = domain.lower()
            results[domain] = pricing_record(domain, item)
        return results
