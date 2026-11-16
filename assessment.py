"""Explainable screening rules; rankings are evidence assessments, not legal clearance."""
import re
from urllib.parse import urlparse

CONFLICT_OPTIONS = ['Use automatic assessment', 'Strong conflict', 'Possible overlap', 'Little evidence']

_B2B_TERMS = {'business', 'company', 'companies', 'lead', 'leads', 'prospect', 'prospects',
              'prospecting', 'research', 'sales', 'market', 'markets', 'insight', 'insights',
              'data', 'account', 'accounts'}
_GENERIC_TERMS = _B2B_TERMS | {'app', 'finder', 'find', 'platform', 'search', 'service',
                               'services', 'software', 'solution', 'solutions', 'system',
                               'systems', 'tech', 'technology', 'tool', 'tools'}


def brand_priority(project, name, review):
    """Return transparent business-name ordering signals, not claims about brand quality."""
    from engine import RATING_FIELDS, rating_score
    if project.get('platform') != 'Business / other':
        return 0, 'Business-name signals not applied to this project.'
    separated = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
    separated = re.sub(r'([A-Z])([A-Z][a-z])', r'\1 \2', separated)
    tokens = re.findall(r'[^\W_]+', separated.casefold(), flags=re.UNICODE)
    topic = 'present' if any(token in _B2B_TERMS for token in tokens) else 'absent'
    topic_detail = f'Prospecting/research term {topic} (not scored).'
    rating = rating_score(review)
    if rating is not None:
        detail = ', '.join(f'{field} {review[field]}/5' for field in RATING_FIELDS)
        return rating // 5, 'User ratings: ' + detail + '. ' + topic_detail
    length = sum(character.isalnum() for character in name)
    length_points = 5 if 5 <= length <= 18 else 2 if 19 <= length <= 24 else 0
    format_points = 5 if re.fullmatch(r'[^\W\d_]+(?:[ -][^\W\d_]+)*', name.strip(), flags=re.UNICODE) else 0
    distinctive_points = 5 if any(token not in _GENERIC_TERMS and token.isalpha() for token in tokens) else 0
    detail = (f'Unrated name-format signals: length {length_points}/5, simple formatting {format_points}/5, '
              f'non-generic token {distinctive_points}/5. {topic_detail} '
              'These are proxies, not proof of memorability, distinctiveness or suitability.')
    return length_points + format_points + distinctive_points, detail


def default_profile(platform):
    business = platform == 'Business / other'
    podcast = platform == 'Podcast'
    return {'youtube': not business, 'gmail': not business, 'social': not business,
            'podcasts': podcast, 'terms': 'business, software, consulting' if business else
            'podcast, audio, show' if podcast else 'gardening, gaming, islands, simulation',
            'secondary': 'Domain / business' if business else 'Podcast name' if podcast else 'YouTube / Gmail'}


def profile_for(project):
    base = default_profile(project.get('platform', 'Business / other'))
    text = (project.get('brief', '') + ' ' + project.get('audience', '')).casefold()
    if project.get('platform') == 'Business / other' and re.search(r'bank|lending|finance', text):
        base['terms'] = 'banking, banker, lending, software, consulting'
    return base | project.get('research_profile', {})


def assess(project, name, row):
    from engine import PLATFORMS, effective_status, targets_for, classify_match, canonical_url, normalize, brand_identity
    from domain_pricing import pricing_fields, valid_domain
    profile = profile_for(project)
    targets = targets_for(project, name)
    review = project['reviews'].get(name.casefold(), {})
    fields, statuses = {}, {}
    for platform in PLATFORMS:
        status, reason, checked = effective_status(project, name, platform, row)
        if platform == 'Domain' and not valid_domain(targets['Domain'].strip().lower()):
            status, reason, checked = 'Not checked', 'Routine screening requires a .com target; historical data remains saved.', ''
        if platform == 'YouTube' and not profile['youtube']:
            status, reason = 'Not selected', 'YouTube is off for this project.'
        if platform == 'Gmail' and not profile['gmail']:
            status, reason = 'Not selected', 'Gmail is off for this project.'
        if platform in ('Facebook', 'TikTok', 'Instagram') and not profile['social']:
            status, reason = 'Not selected', 'Social searches are off for this project.'
        if platform == 'Domain' and status in ('Resolves', 'No A record', 'Unknown', 'Failed'):
            status = 'Not checked' if not row or not row.get('registration') else 'Check failed'
            reason = 'No current registry lookup. Run research; DNS alone does not establish registration.'
        statuses[platform] = status
        fields.update({platform + ' status': status, platform + ' target': targets[platform],
                       platform + ' reason': reason, platform + ' checked at': checked})
    checks = {c['source']: c for c in (row or {}).get('checks', [])}
    required = ['Google', 'Google broad', 'Industry search', 'Domain registration']
    if profile['youtube']:
        required += ['YouTube', 'YouTube handle']
    if profile['podcasts']:
        required += ['Apple Podcasts']
    completed, missing = [], []
    for source in required:
        if source == 'Domain registration':
            status = statuses['Domain']
            okay = status in ('Registered', 'Potentially available', 'Confirmed available', 'Owned by me')
            message = fields['Domain reason']
            check = (row or {}).get('registration', {})
        elif source == 'YouTube handle':
            check = (row or {}).get('youtube_handle', {})
            okay = check.get('status') in ('Handle found', 'No channel returned') and (row or {}).get('targets', targets).get('YouTube') == targets['YouTube']
            message = check.get('message', 'Not researched yet.')
        else:
            check = checks.get(source, {})
            okay = check.get('status') == 'Completed' and not check.get('details_error')
            message = check.get('details_error') or check.get('message', 'Not researched yet.')
            if source == 'Industry search' and check.get('profile_terms', (row or {}).get('profile', {}).get('terms')) != profile['terms']:
                okay = False
                message = 'Industry terms changed or this is an older run. Research again.'
        if check.get('retry_needed'):
            okay = False
            attempt = check.get('last_attempt', {})
            message = ('Latest attempt ' + attempt.get('checked_at', '') + ': ' + attempt.get('message', 'Incomplete check.')
                       + '; retained evidence from ' + check.get('checked_at', 'unknown date') + '.')
        if okay:
            completed.append(source)
        else:
            missing.append(source + ': ' + message)
    # Industry searches from a different profile cannot establish present-day conflicts.
    industry_current = checks.get('Industry search', {}).get('profile_terms', (row or {}).get('profile', {}).get('terms')) == profile['terms']
    terms = [x.strip().casefold() for x in profile['terms'].split(',') if x.strip()]
    evidence, seen = [], {}
    sources = ['Google', 'Google broad', 'Industry search']
    if profile['youtube']:
        sources.append('YouTube')
    if profile['podcasts']:
        sources.append('Apple Podcasts')
    if profile['social']:
        sources += ['Facebook', 'Instagram', 'TikTok']
    for source in sources:
        if source == 'Industry search' and not industry_current:
            continue
        for match in checks.get(source, {}).get('matches', []):
            title, description, url = match.get('title', ''), match.get('description', ''), match.get('url', '')
            kind, hit_fields = classify_match(name, title, description, url, match.get('handle', ''))
            if kind == 'Review relevance':
                continue
            identity = canonical_url(url) or normalize(title)
            text = (title + ' ' + description).casefold()
            context = [t for t in terms if re.search(r'(?<!\w)' + re.escape(t) + r'(?!\w)', text)]
            parsed_url = urlparse(url)
            path = parsed_url.path.rstrip('/')
            # A matching brand title with relevant industry language on a homepage,
            # app/podcast listing or selected YouTube channel is a stronger lead.
            entity_page = source in ('YouTube', 'Apple Podcasts') or path in ('', '/about', '/about-us') or '/app/' in path or 'play.google.com/store/apps' in url
            strong = (kind in ('Exact title', 'Name in title') and brand_identity(name, title, url)
                      and bool(context) and entity_page and parsed_url.scheme in ('http', 'https')
                      and bool(parsed_url.netloc) and checks[source].get('status') == 'Completed'
                      and not checks[source].get('details_error'))
            lead = {'source': source, 'title': title, 'description': description, 'url': url,
                    'match': kind, 'matched_fields': hit_fields, 'context': ', '.join(context),
                    'assessment': 'Strong conflict lead' if strong else 'Possible overlap'}
            if identity in seen:
                if strong:
                    index = seen[identity]
                    if evidence[index]['assessment'] != 'Strong conflict lead':
                        evidence[index] = lead
                continue
            seen[identity] = len(evidence)
            evidence.append(lead)
    strong = [m for m in evidence if m['assessment'] == 'Strong conflict lead']
    web_complete = all(x in completed for x in ['Google', 'Google broad', 'Industry search'])
    automatic = 'Strong conflict' if strong else 'Possible overlap' if evidence else 'Little evidence' if web_complete else 'Insufficient evidence'
    override = review.get('conflict_assessment', 'Use automatic assessment')
    conflict = override if override in CONFLICT_OPTIONS[1:] else automatic
    fields['Automatic conflict assessment'] = automatic
    fields['Conflict assessment'] = conflict
    fields['Assessment basis'] = 'Manual review: ' + review.get('conflict_evidence', '') if override in CONFLICT_OPTIONS[1:] else 'Automatic screening of title, snippet, URL, context terms and page type; review source evidence.'
    fields['Competing names'] = '; '.join(dict.fromkeys(m['title'] for m in evidence))
    fields['Conflict links'] = '\n'.join(m['url'] for m in evidence)
    fields['Conflict evidence'] = evidence
    fields['Relevant conflict leads'] = len(strong)
    fields['Overlap leads'] = len(evidence)
    available = {'Confirmed available', 'Owned by me'}
    domain = statuses['Domain']
    pricing = pricing_fields(project, name, research_row=row)
    eligibility = pricing['Domain eligibility']
    if domain == 'Registered' or pricing['Registration availability'] in ('Registered', 'Unavailable') or eligibility in ('Over budget', 'Not eligible'):
        fields['Domain points'] = 0
    elif eligibility == 'Eligible':
        fields['Domain points'] = 30
    elif eligibility == 'Promising':
        fields['Domain points'] = 20
    else:
        fields['Domain points'] = 10 if domain in available | {'Potentially available'} else 0
    fields['Conflict points'] = {'Little evidence': 35, 'Possible overlap': 15}.get(conflict, 0)
    fields['Coverage points'] = round(15 * len(completed) / len(required))
    fields['Universal score'] = fields['Domain points'] + fields['Conflict points'] + fields['Coverage points']
    yt, gm = statuses['YouTube'], statuses['Gmail']
    fields['YouTube points'] = (8 if yt in available else 4 if yt == 'No match in sample' else 0) if profile['youtube'] else 0
    fields['Gmail points'] = 4 if profile['gmail'] and gm in available else 0
    for platform in ('Facebook', 'TikTok', 'Instagram'):
        fields[platform + ' points'] = (1 if statuses[platform] in available else .5 if statuses[platform] == 'No match in sample' else 0) if profile['social'] else 0
    apple = checks.get('Apple Podcasts', {})
    podcast_hits = [m for m in apple.get('matches', []) if classify_match(name, m.get('title', ''))[0] in ('Exact title', 'Name in title', 'Similar title')]
    fields['Podcast points'] = 5 if profile['podcasts'] and apple.get('status') == 'Completed' and not podcast_hits else 0
    fields['Project points'] = sum(fields[p + ' points'] for p in ('YouTube', 'Gmail', 'Facebook', 'TikTok', 'Instagram')) + fields['Podcast points']
    fields['Priority score'] = fields['Universal score'] + fields['Project points']
    fields['Brand priority points'], fields['Brand priority basis'] = brand_priority(project, name, review)
    fields['YouTube tier'] = 3 if yt in available else 2 if yt == 'No match in sample' else 0 if yt in ('Conflict found', 'Name conflict found') else 1
    fields['Gmail tier'] = 2 if gm in available else 0 if gm in ('Conflict found', 'Invalid Gmail username') else 1
    fields['Web conflict penalty'] = 0  # Retained for compatibility; conflicts now have their own component.
    fields['Ranking confidence'] = 'Research complete — limited samples' if not missing else f'Incomplete: {len(completed)}/{len(required)} required checks'
    fields['Incomplete checks'] = '\n'.join(missing)
    fields['Completed checks'] = ', '.join(completed)
    records = [(check['source'], check) for check in (row or {}).get('checks', [])]
    records += [(source, (row or {})[key]) for source, key in
                (('Domain registration', 'registration'), ('Domain DNS', 'dns'), ('YouTube handle', 'youtube_handle'))
                if (row or {}).get(key)]
    fields['Research evidence dates'] = '\n'.join(
        source + ': ' + check.get('checked_at', (row or {}).get('checked_at', 'Unknown'))
        for source, check in records)
    fields['Research refresh warnings'] = '\n'.join(
        failure['source'] + ': latest attempt ' + failure['checked_at'] + ' — ' + failure['message']
        + (' Retained evidence: ' + failure['evidence_checked_at'] + '.' if failure['evidence_checked_at'] else '')
        for failure in (row or {}).get('refresh_failures', []))
    def recommendation_for(conflict_value, manual=False):
        if conflict_value == 'Strong conflict':
            return ('Reject', 'Manual conflict assessment: strong conflict; review the saved evidence before a final decision.'
                    if manual else 'Strong competing-brand lead in the selected context; inspect the linked evidence before a final decision.')
        if (conflict_value in ('Possible overlap', 'Insufficient evidence') or missing
                or domain not in available | {'Potentially available'} or eligibility not in ('Eligible', 'Promising')):
            return ('Investigate', 'Resolve overlap or incomplete research.' if missing or conflict_value != 'Little evidence'
                    else pricing['Verification needed'])
        return 'Shortlist', 'Little competing-name evidence in completed samples. ' + pricing['Verification needed']

    fields['Automatic recommendation'] = recommendation_for(automatic)[0]
    recommendation, reason = recommendation_for(conflict, manual=override == 'Strong conflict')
    fields['Recommendation'] = recommendation
    fields['Recommendation reason'] = reason
    fields['Ranking reason'] = (f'{recommendation}; evidence {fields["Universal score"]}/80 '
                                f'(domain {fields["Domain points"]}/30, conflict {fields["Conflict points"]}/35, '
                                f'coverage {fields["Coverage points"]}/15); business-name priority '
                                f'{fields["Brand priority points"]} points; optional project points {fields["Project points"]}. '
                                f'{fields["Brand priority basis"]} {reason}')
    if strong and conflict == 'Strong conflict':
        action = 'Review strongest conflict: ' + strong[0]['title']
    elif missing:
        action = 'Complete ' + missing[0].split(':', 1)[0] + ' — see diagnostics'
    elif domain == 'Registered':
        action = 'Investigate the .com registration and any resale listing'
    elif conflict == 'Possible overlap':
        action = 'Review the linked overlap evidence'
    elif domain == 'Potentially available':
        action = 'Confirm domain eligibility and price at a registrar'
    else:
        action = 'Compare shortlisted names and confirm optional accounts'
    fields['Next action'] = action
    fields['Research'] = 'Research incomplete' if missing else 'Completed required checks'
    return fields
