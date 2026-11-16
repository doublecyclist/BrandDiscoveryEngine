"""Read-only registrar quotes, independent of brand research and decisions."""
import ipaddress
import hashlib
import csv
import io
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from time import monotonic, sleep

import requests

from engine import latest_results, now, targets_for

MAX_BUDGET = 100
CACHE_SECONDS = 24 * 60 * 60
AFFORDABLE = 'Affordable'
OVER_BUDGET = 'Over budget'
PREMIUM_UNKNOWN = 'Premium \u2014 price unknown'
RESALE_UNKNOWN = 'Registered \u2014 resale unknown'
UNAVAILABLE = 'Price unavailable'
PROVISIONAL = 'Provisional \u2014 total unverified'
NS = {'nc': 'http://api.namecheap.com/xml.response'}
RESULT_FILTERS = ('All checked domains', 'Provisional under $100', 'Verified affordable',
                  'Over budget', 'Unavailable or unknown price')
RESULT_COLUMNS = ('Candidate names', 'Domain name', 'Registration availability',
                  'Registration price (USD)', 'Premium/resale purchase fee (USD)',
                  'Acquisition price (USD)', 'Annual renewal price (USD)',
                  'Budget classification', 'Price-check date', 'Price reliability',
                  'Pricing currency', 'Pricing source', 'Pricing notes')


class PricingError(RuntimeError):
    """A safe-to-display pricing failure, without credentials or raw responses."""


def amount(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number >= 0 else None


def budget_for(project):
    value = amount(project.get('domain_budget', MAX_BUDGET))
    return float(min(value, Decimal(MAX_BUDGET))) if value is not None else MAX_BUDGET


def valid_domain(domain):
    return bool(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.com', domain))


def fresh(record):
    try:
        checked = datetime.fromisoformat(record['checked_at'])
        age = (datetime.now(timezone.utc) - checked).total_seconds()
        return 0 <= age < CACHE_SECONDS
    except (KeyError, TypeError, ValueError):
        return False


def classification(record, budget=MAX_BUDGET):
    price = amount(record.get('acquisition_price'))
    reliable = (record.get('verified') is True and record.get('currency') == 'USD'
                and fresh(record) and bool(record.get('source'))
                and record.get('purchase_available') is not False
                and ((record.get('availability') == 'Available' and record.get('price_kind') == 'registration')
                     or (record.get('availability') == 'Registered' and record.get('price_kind') == 'resale')))
    if reliable and price is not None:
        limit = amount(budget)
        if limit is None:
            raise ValueError('Enter a non-negative domain budget.')
        if price > min(limit, Decimal(MAX_BUDGET)):
            return OVER_BUDGET
        return AFFORDABLE if record.get('total_verified') is True else PROVISIONAL
    # Even an incomplete quote can establish that the known minimum is over budget.
    lower_bound = amount(record.get('acquisition_lower_bound'))
    if (record.get('currency') == 'USD' and fresh(record) and record.get('purchase_available') is True
            and lower_bound is not None and lower_bound > min(amount(budget) or Decimal(0), Decimal(MAX_BUDGET))):
        return OVER_BUDGET
    if record.get('availability') == 'Unavailable':
        return RESALE_UNKNOWN
    if record.get('availability') == 'Registered':
        return RESALE_UNKNOWN
    if record.get('premium') is True:
        return PREMIUM_UNKNOWN
    return UNAVAILABLE


def pricing_fields(project, name, domain=None):
    domain = domain if domain is not None else targets_for(project, name)['Domain'].strip().lower()
    record = project.get('domain_pricing', {}).get(domain, {})
    research = latest_results(project).get(name.casefold(), {})
    registration = research.get('registration', {})
    prior_status = registration.get('status', 'Unknown') if registration.get('domain', '').lower() == domain else 'Unknown'
    effective = record or {'availability': prior_status}
    def usd_price(field):
        value = amount(record.get(field))
        return float(value) if value is not None and record.get('currency') == 'USD' else None

    return {
        'Domain name': domain,
        'Registration availability': record.get('availability', prior_status),
        'Premium status': {True: 'Yes', False: 'No', None: 'Unknown'}.get(record.get('premium'), 'Unknown'),
        'Acquisition price (USD)': usd_price('acquisition_price'),
        'Annual renewal price (USD)': usd_price('annual_renewal_price'),
        'Budget classification': classification(effective, budget_for(project)),
        'Price-check date': record.get('checked_at', ''),
        'Pricing source': record.get('source', ''),
        'Pricing notes': record.get('message', ''),
        'Registration price (USD)': usd_price('registration_price'),
        'Premium/resale purchase fee (USD)': usd_price('premium_purchase_fee'),
        'Known acquisition minimum (USD)': usd_price('acquisition_lower_bound'),
        'Taxes and mandatory charges': record.get('charges_status', 'Not fully verified'),
        'Price reliability': ('Stale' if record and not fresh(record) else
                              'Final total verified' if record.get('total_verified') is True else
                              'Provisional' if record.get('verified') is True else 'Unknown'),
        'Pricing currency': record.get('currency', ''),
        'Registrar inventory': record.get('inventory', ''),
    }


def cached_pricing_rows(project):
    """One row per saved domain, including quotes for former candidate targets."""
    names_by_domain = {}
    for name in project['names']:
        domain = targets_for(project, name)['Domain'].strip().lower()
        names_by_domain.setdefault(domain, []).append(name)
    rows = []
    for domain, record in project.get('domain_pricing', {}).items():
        if not record:
            continue
        names = names_by_domain.get(domain, [])
        fields = pricing_fields(project, names[0] if names else '', domain=domain)
        row = {'Candidate names': '; '.join(names) if names else '(not in current candidate list)', **fields}
        rows.append({column: row[column] for column in RESULT_COLUMNS})
    rows.sort(key=lambda row: (
        row['Budget classification'] != PROVISIONAL,
        row['Acquisition price (USD)'] is None,
        row['Acquisition price (USD)'] if row['Acquisition price (USD)'] is not None else 0,
        row['Domain name']))
    return rows


def filter_pricing_rows(rows, selected='Provisional under $100', search=''):
    if selected not in RESULT_FILTERS:
        raise ValueError('Choose a supported domain pricing filter.')
    categories = {'Provisional under $100': {PROVISIONAL}, 'Verified affordable': {AFFORDABLE},
                  'Over budget': {OVER_BUDGET},
                  'Unavailable or unknown price': {UNAVAILABLE, RESALE_UNKNOWN, PREMIUM_UNKNOWN}}
    needle = search.strip().casefold()
    return [row for row in rows
            if (selected == 'All checked domains' or row['Budget classification'] in categories[selected])
            and (not needle or any(needle in str(value).casefold() for value in row.values() if value is not None))]


def pricing_results_csv(rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=RESULT_COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: ("'" + value if isinstance(value, str)
                                and value.lstrip().startswith(('=', '+', '-', '@')) else value)
                         for key, value in row.items()})
    return output.getvalue().encode('utf-8-sig')


def plan_prices(project, shortlisted=True, refresh=False, provider=None):
    domains, invalid = [], []
    for name in project['names']:
        if shortlisted and project['reviews'].get(name.casefold(), {}).get('decision') not in ('Keep', 'Maybe'):
            continue
        domain = targets_for(project, name)['Domain'].strip().lower()
        if not valid_domain(domain):
            invalid.append(name)
        elif domain not in domains:
            domains.append(domain)
    cache = project.get('domain_pricing', {})
    pending = [domain for domain in domains if not cache.get(domain, {}).get('manual')
               and (refresh or not fresh(cache.get(domain, {}))
                    or (provider is not None and cache.get(domain, {}).get('provider') != provider))]
    return domains, pending, invalid


def observed_quote(domain, availability, premium, price, renewal, source, total_verified=False):
    """Record an actual listing, never an estimate or a standard-price assumption."""
    if not valid_domain(domain):
        raise ValueError('Enter a valid candidate .com domain.')
    if availability not in ('Available', 'Registered', 'Unknown') or premium not in (True, False, None):
        raise ValueError('Choose a registration and premium status.')
    if not source.strip():
        raise ValueError('Enter the registrar/listing source and URL or reference.')
    values = []
    for label, value in (('Acquisition', price), ('Annual renewal', renewal)):
        parsed = amount(value) if str(value).strip() else None
        if str(value).strip() and parsed is None:
            raise ValueError(label + ' price must be a non-negative USD amount, without $ or commas.')
        values.append(str(parsed) if parsed is not None else None)
    if total_verified and (values[0] is None or availability == 'Unknown'):
        raise ValueError('A verified final total requires a known price and purchase availability.')
    return {'domain': domain, 'availability': availability, 'premium': premium,
            'acquisition_price': values[0], 'annual_renewal_price': values[1],
            'currency': 'USD', 'price_kind': 'resale' if availability == 'Registered' else 'registration',
            'verified': values[0] is not None and availability != 'Unknown', 'manual': True,
            'total_verified': total_verified is True and values[0] is not None and availability != 'Unknown',
            'charges_status': 'User-confirmed all-in total' if total_verified else 'Taxes/mandatory charges not fully verified',
            'checked_at': now(), 'source': 'User-observed registrar listing: ' + source.strip(),
            'message': 'User-confirmed asking price including mandatory listed fees; checkout taxes may apply.'}


class NamecheapPricing:
    """Only two allowlisted, non-purchasing Namecheap API methods."""
    batch_size = 50
    requires_tariff = True

    def __init__(self):
        self.user = os.getenv('NAMECHEAP_API_USER', '').strip()
        self.key = os.getenv('NAMECHEAP_API_KEY', '').strip()
        self.ip = os.getenv('NAMECHEAP_CLIENT_IP', '').strip()
        if os.getenv('NAMECHEAP_FREE_API_CONFIRMED') != '1':
            raise PricingError('Live pricing stopped: confirm existing no-additional-fee API access with '
                               'NAMECHEAP_FREE_API_CONFIRMED=1. Do not fund an account or buy API access for this check.')
        if not all((self.user, self.key, self.ip)):
            raise PricingError('Namecheap needs NAMECHEAP_API_USER, NAMECHEAP_API_KEY and '
                               'NAMECHEAP_CLIENT_IP (your whitelisted public IPv4 address).')
        try:
            ipaddress.IPv4Address(self.ip)
        except ipaddress.AddressValueError:
            raise PricingError('NAMECHEAP_CLIENT_IP must be a whitelisted public IPv4 address.') from None
        self.last_request = None
        self.account_id = hashlib.sha256(self.user.encode()).hexdigest()

    def request(self, command, **params):
        if command not in ('namecheap.domains.check', 'namecheap.users.getPricing'):
            raise PricingError('Only read-only pricing commands are supported.')
        if self.last_request is not None:
            sleep(max(0, 1.3 - (monotonic() - self.last_request)))
        self.last_request = monotonic()
        try:
            response = requests.get(
                'https://api.namecheap.com/xml.response',
                params={**params, 'ApiUser': self.user, 'ApiKey': self.key, 'UserName': self.user,
                        'ClientIp': self.ip, 'Command': command},
                timeout=(4, 12), allow_redirects=False)
        except requests.Timeout:
            raise PricingError('Namecheap pricing timed out. No automatic retry; try again later.') from None
        except requests.RequestException:
            raise PricingError('Could not reach Namecheap pricing. No automatic retry.') from None
        if response.status_code == 429:
            wait = response.headers.get('Retry-After', '')
            seconds = wait if wait.isdigit() else 'the provider-recommended interval'
            raise PricingError(f'Namecheap rate limit reached. Retry after {seconds} seconds; no retry was sent.')
        if response.status_code in (401, 402, 403):
            raise PricingError('Namecheap access denied or payment/access required. Stopped; '
                               'verify existing free API eligibility, credentials and IP whitelist. No payment attempted.')
        if response.status_code != 200:
            raise PricingError(f'Namecheap returned HTTP {response.status_code}. Pricing stopped.')
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            raise PricingError('Namecheap returned invalid XML. Pricing is unavailable.') from None
        errors = root.findall('.//nc:Error', NS)
        if root.get('Status') != 'OK' or errors:
            codes = ', '.join(e.get('Number', '') for e in errors if e.get('Number', '').isdigit())
            raise PricingError('Namecheap API rejected the lookup' + (f' (code {codes})' if codes else '') +
                               '. Check access and rate limits; no automatic retry or payment.')
        return root

    def standard_prices(self):
        root = self.request('namecheap.users.getPricing', ProductType='DOMAIN',
                            ProductCategory='DOMAINS', ProductName='COM')
        prices = {}
        for action in ('REGISTER', 'RENEW'):
            for category in root.findall('.//nc:ProductCategory', NS):
                if category.get('Name', '').upper() != action:
                    continue
                for product in category.findall('nc:Product', NS):
                    if product.get('Name', '').lower() != 'com':
                        continue
                    for row in product.findall('nc:Price', NS):
                        if row.get('Duration') != '1' or row.get('DurationType') != 'YEAR' or row.get('Currency') != 'USD':
                            continue
                        price = amount(row.get('YourPrice', row.get('Price')))
                        fee = amount(row.get('YourAdditionalCost', row.get('AdditionalCost')))
                        if price is not None and price > 0 and fee is not None:
                            prices[action] = str(price + fee)
        return prices

    def check_batch(self, domains, standard):
        if not domains or len(domains) > self.batch_size or any(not valid_domain(d) for d in domains):
            raise PricingError('Namecheap checks require 1 to 50 valid .com domains per batch.')
        root = self.request('namecheap.domains.check', DomainList=','.join(domains))
        found = {}
        for row in root.findall('.//nc:DomainCheckResult', NS):
            domain = row.get('Domain', '').lower()
            if domain not in domains:
                continue
            available = row.get('Available', '').lower()
            premium = {'true': True, 'false': False}.get(row.get('IsPremiumName', '').lower())
            result = {'domain': domain, 'availability': 'Unknown', 'premium': premium,
                      'acquisition_price': None, 'annual_renewal_price': None, 'currency': 'USD',
                      'price_kind': 'registration', 'verified': False, 'checked_at': now(),
                      'source': 'Namecheap production API', 'provider': 'Namecheap', 'total_verified': False,
                      'charges_status': 'Returned fees included; taxes not returned', 'message': ''}
            if row.get('ErrorNo') != '0' or available not in ('true', 'false'):
                result['message'] = 'Registrar returned a domain-level error or incomplete availability.'
            else:
                result['availability'] = 'Available' if available == 'true' else 'Registered'
                if available == 'false':
                    result['message'] = 'No verified resale quote supplied by this API; check the registrar listing.'
                elif premium is True:
                    price = amount(row.get('PremiumRegistrationPrice'))
                    renewal = amount(row.get('PremiumRenewalPrice'))
                    icann, eap = amount(row.get('IcannFee')), amount(row.get('EapFee'))
                    if price is not None and price > 0 and icann is not None and eap is not None:
                        result['acquisition_price'] = str(price + icann + eap)
                    if renewal is not None and renewal > 0 and icann is not None:
                        result['annual_renewal_price'] = str(renewal + icann)
                elif premium is False:
                    result['acquisition_price'] = standard.get('REGISTER')
                    result['annual_renewal_price'] = standard.get('RENEW')
                result['verified'] = result['acquisition_price'] is not None
                if available == 'true':
                    result['message'] = ('One-year quote including returned mandatory fees; checkout taxes may apply.'
                                         if result['verified'] else 'No reliable one-year USD quote including fees returned.')
            found[domain] = result
        if set(found) != set(domains):
            raise PricingError('Namecheap omitted requested domains. This batch was not saved; try later.')
        return found


def check_prices(project, domains, provider, persist, progress=None):
    """Persist each successful batch; never touch reviews, targets or research."""
    if any(not valid_domain(d) for d in domains):
        raise PricingError('Pricing accepts valid .com domains only.')
    cache = project.get('domain_pricing', {})
    domains = [d for d in dict.fromkeys(domains) if not cache.get(d, {}).get('manual')]
    if not domains:
        return 0
    cache = project.setdefault('domain_pricing', {})
    # A cached tariff is account-specific; it is never shared across projects/users.
    tariff = {}
    if provider.requires_tariff:
        tariff = project.get('domain_pricing_tariff', {})
        if not fresh(tariff) or tariff.get('account') != provider.account_id:
            tariff = {'checked_at': now(), 'account': provider.account_id, 'prices': provider.standard_prices()}
            project['domain_pricing_tariff'] = tariff
            if not persist():
                raise PricingError('Could not save pricing. Stopped before domain lookups.')
    count = 0
    if progress:
        progress(count, len(domains))
    for start in range(0, len(domains), provider.batch_size):
        batch = domains[start:start + provider.batch_size]
        results = provider.check_batch(batch, tariff.get('prices', {}))
        for result in results.values():
            if provider.requires_tariff and result.get('verified') and result.get('premium') is False:
                result['checked_at'] = tariff['checked_at']
        for domain, result in results.items():
            previous = cache.get(domain)
            if previous and previous != result:
                project.setdefault('domain_pricing_history', {}).setdefault(domain, []).append(previous.copy())
        cache.update(results)
        if not persist():
            raise PricingError('Could not save domain prices. Download a backup; no further requests were sent.')
        count += len(batch)
        if progress:
            progress(count, len(domains))
    return count
