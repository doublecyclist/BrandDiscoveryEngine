"""Explicit, GET-only v3 probe. Never writes project data or raw responses."""
import argparse
import getpass
import json
import os
from decimal import Decimal
from pathlib import Path

import requests

from domain_pricing import valid_domain

ENDPOINT = 'https://api.godaddy.com/v3/domains/check-availability'
DEFAULT_DOMAINS = ('harborglyph.com', 'tidecanvas.com', 'lanterntrail.com')
CURRENCY_DIGITS = {'USD': 2, 'EUR': 2, 'GBP': 2, 'CAD': 2, 'AUD': 2,
                   'JPY': 0, 'KRW': 0, 'KWD': 3, 'BHD': 3, 'OMR': 3, 'TND': 3}


class ProbeError(RuntimeError):
    """Only fixed, credential-free messages may cross the CLI boundary."""


def money(value):
    if not isinstance(value, dict):
        return None
    currency, units = value.get('currencyCode'), value.get('value')
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isascii() or not currency.isupper():
        raise ProbeError('Unexpected currency in pricing response; stopped.')
    if type(units) is not int or units < 0:
        raise ProbeError('Unexpected minor-unit price in response; stopped.')
    digits = CURRENCY_DIGITS.get(currency)
    return {'currency': currency, 'minor_units': units,
            'amount': format(Decimal(units).scaleb(-digits), f'.{digits}f') if digits is not None else None}


def summarize(domain, data):
    if not isinstance(data, dict) or not isinstance(data.get('domain'), str) or data['domain'].lower() != domain:
        raise ProbeError('Unexpected domain response; stopped without displaying the raw response.')
    if data.get('error') is not None:
        raise ProbeError('GoDaddy returned a domain-level error (HTTP 200). '
                         'The domain could not be checked; no retry sent.')
    available = data.get('available')
    if type(available) is not bool:
        raise ProbeError('GoDaddy omitted a valid availability result; stopped.')
    inventory = data.get('inventory')
    if inventory not in ('REGISTRY', 'REGISTRY_PREMIUM', 'PREMIUM'):
        inventory = None
    prices = data.get('prices', [])
    if not isinstance(prices, list):
        raise ProbeError('Unexpected pricing response; stopped.')
    annual_prices = [p for p in prices if isinstance(p, dict)
                     and p.get('term') == 'YEAR' and type(p.get('period')) is int and p['period'] == 1]
    if len(annual_prices) > 1:
        raise ProbeError('GoDaddy returned ambiguous one-year prices; stopped.')
    annual = annual_prices[0] if annual_prices else {}
    registration = money(annual.get('price'))
    renewal = money(annual.get('renewalPrice'))
    raw_fees = annual.get('fees', [])
    if not isinstance(raw_fees, list):
        raise ProbeError('Unexpected premium fee response; stopped.')
    fees = []
    for item in raw_fees:
        if not isinstance(item, dict) or money(item.get('fee')) is None:
            raise ProbeError('Incomplete premium fee response; stopped.')
        fees.append(money(item['fee']))
    total = None
    if (available and registration and registration['amount'] is not None
            and inventory is not None and (inventory != 'PREMIUM' or fees)):
        if all(f['currency'] == registration['currency'] and f['amount'] is not None for f in fees):
            total = format(sum((Decimal(f['amount']) for f in fees), Decimal(registration['amount'])), 'f')
            if inventory in ('PREMIUM', 'REGISTRY_PREMIUM') and Decimal(total) == 0:
                total = None
    premium = True if inventory in ('REGISTRY_PREMIUM', 'PREMIUM') or fees else (
        False if inventory == 'REGISTRY' else None)
    return {'domain': domain, 'available': available,
            'definitive': data.get('definitive') if type(data.get('definitive')) is bool else None,
            'inventory': inventory, 'premium': premium,
            'registration_price': registration, 'annual_renewal_price': renewal,
            'premium_purchase_fees': fees, 'indicative_acquisition_excluding_tax_and_icann': total,
            'currency': registration['currency'] if registration else None,
            'resale_listing': inventory == 'PREMIUM',
            'budget_verified': False,
            'notes': 'Indicative only; excludes ICANN fee and taxes. Missing renewal/premium fields remain unknown. '
                     'Availability in marketplace inventory does not mean standard registration.'}


def probe(domains, token):
    domains = list(dict.fromkeys(d.strip().lower() for d in domains))
    if not 2 <= len(domains) <= 3 or any(not valid_domain(d) for d in domains):
        raise ProbeError('Specify exactly two or three distinct valid .com domains.')
    if not token or any(c.isspace() for c in token):
        raise ProbeError('Set GODADDY_PAT securely or use --prompt-token. Never paste the token into chat.')
    results = []
    # A dedicated session has no retry adapters, redirects, or logging enabled.
    with requests.Session() as session:
        for domain in domains:
            try:
                response = session.get(ENDPOINT, params={'domain': domain},
                                       headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'},
                                       timeout=(4, 12), allow_redirects=False)
            except requests.Timeout:
                raise ProbeError('GoDaddy lookup timed out; stopped with no automatic retry.') from None
            except requests.RequestException:
                raise ProbeError('GoDaddy network request failed; stopped with no automatic retry.') from None
            if response.status_code != 200:
                messages = {
                    401: 'Authentication failed: PAT missing, invalid or expired.',
                    402: 'Payment/access required. Stopped; no paid operation attempted.',
                    403: 'Authorization denied: verify domains.domain:read scope / Domain Search access and account eligibility.',
                    429: 'Rate limited. Wait for the provider interval before a separate manual run.',
                }
                raise ProbeError(f'GoDaddy HTTP {response.status_code}: ' +
                                 messages.get(response.status_code, 'Request rejected or provider failure.') +
                                 ' No retry or further domain requests sent.')
            try:
                data = response.json()
            except ValueError:
                raise ProbeError('GoDaddy returned invalid JSON; stopped without displaying it.') from None
            results.append(summarize(domain, data))
    return results


def local_token(prompt=False):
    if prompt:
        return getpass.getpass('GoDaddy PAT (hidden; local terminal only): ').strip()
    token = os.getenv('GODADDY_PAT', '').strip()
    if token:
        return token
    path = Path(__file__).resolve().parent / '.env.godaddy'
    if not path.exists():
        return ''
    try:
        lines = path.read_text(encoding='utf-8-sig').splitlines()
    except (OSError, UnicodeError):
        raise ProbeError('Could not read the local .env.godaddy token file.') from None
    values = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if key.strip() != 'GODADDY_PAT' or not separator:
            raise ProbeError('.env.godaddy must contain only GODADDY_PAT=<token> and optional comments.')
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        values.append(value)
    if len(values) != 1:
        raise ProbeError('.env.godaddy must contain exactly one GODADDY_PAT entry.')
    return values[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('domains', nargs='*', default=list(DEFAULT_DOMAINS))
    parser.add_argument('--live', action='store_true', help='Explicitly authorize up to three read-only requests')
    parser.add_argument('--prompt-token', action='store_true', help='Enter the token invisibly in your local terminal')
    args = parser.parse_args()
    if not args.live:
        print('No requests sent. Add --live for two or three free read-only availability lookups.')
        return 0
    try:
        results = probe(args.domains, local_token(args.prompt_token))
    except (ProbeError, EOFError, KeyboardInterrupt) as error:
        # Do not print arbitrary exceptions, response bodies or request headers.
        print(str(error) if isinstance(error, ProbeError) else 'Local token entry cancelled; no further requests.')
        return 1
    print(json.dumps({'source': 'GoDaddy v3 GET availability', 'results': results}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
