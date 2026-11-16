"""Brand Discovery Engine 0.4: evidence, project storage, and explicit human ratings."""
import csv
import io
import json
import os
import re
import tempfile
import uuid
import html
import unicodedata
import hashlib
from contextlib import contextmanager
from copy import deepcopy
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse, unquote

import requests

VERSION = '0.6.5'
ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('BRAND_ENGINE_DATA_DIR') or
            (Path(os.environ.get('APPDATA', str(Path.home()))) / 'BrandDiscoveryEngine'
             if os.name == 'nt' else Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'BrandDiscoveryEngine'))
PROJECTS = DATA / 'projects'
RATING_FIELDS = ('Audience fit', 'Memorability', 'Clarity', 'Distinctiveness')
DECISIONS = ['Undecided', 'Keep', 'Maybe', 'Reject']


class ProjectConflict(ValueError):
    """A save cannot replace a project changed since this session loaded it."""


class Project(dict):
    """JSON-compatible project with a session-only file revision, never exported."""
    _revision: str | None = None
    _path: Path | None = None


def now():
    return datetime.now(timezone.utc).isoformat()


def normalize(name):
    return ''.join(c for c in unicodedata.normalize('NFKC', html.unescape(name)).casefold() if c.isalnum())


def slug(name):
    return re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().casefold())


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        revision = hashlib.sha256(Path(temp).read_bytes()).hexdigest()
        os.replace(temp, path)
        return revision
    finally:
        Path(temp).unlink(missing_ok=True)


def load_settings(strict=False):
    try:
        data = json.loads((DATA / 'settings.json').read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('Invalid settings metadata.')
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        if strict:
            raise
        return {}


def save_settings(**values):
    settings = load_settings(strict=True)
    settings.update(values)
    write_json(DATA / 'settings.json', settings)


def stored_key(source):
    env = 'YOUTUBE_API_KEY' if source == 'YouTube' else 'SERPAPI_KEY'
    # Hosted deployments use environment secrets; never persist them to project JSON.
    configured = os.getenv(env, '').strip()
    if configured:
        return configured
    try:
        import keyring
        return keyring.get_password('BrandDiscoveryEngine', source) or ''
    except Exception:
        return os.getenv(env, '')


def remember_keys(youtube, google):
    # Windows Credential Manager, not project JSON or exports.
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError('Install the optional keyring dependency to save keys locally.') from exc
    for source, value in [('YouTube', youtube), ('SerpAPI', google)]:
        if value.strip():
            keyring.set_password('BrandDiscoveryEngine', source, value.strip())
        elif keyring.get_password('BrandDiscoveryEngine', source):
            keyring.delete_password('BrandDiscoveryEngine', source)


def migrate_local_projects():
    """Import projects left next to app.py by 0.4, without overwriting newer data."""
    legacy = ROOT / 'projects'
    if legacy.resolve() == PROJECTS.resolve() or not legacy.exists():
        return
    for path in legacy.glob('*.json'):
        try:
            p = load_project(path)
            dest = project_path(p['id'])
            if not dest.exists():
                save_project(p)
        except (OSError, ValueError, KeyError, TypeError):
            continue


def resume_project_path():
    """Find the exact saved file without renaming or rewriting it."""
    settings = load_settings()
    if settings.get('last_project_filename'):
        try:
            path = project_file_path(settings['last_project_filename'])
            load_project(path)
            return path
        except (OSError, ValueError, KeyError, TypeError):
            pass
    if settings.get('last_project'):
        try:
            path = project_path(settings['last_project'])
            load_project(path)
            return path
        except (OSError, ValueError, KeyError, TypeError):
            pass
    paths = sorted(PROJECTS.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True) if PROJECTS.exists() else []
    for path in paths:
        try:
            load_project(path)
            return path
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return None


def resume_project():
    migrate_local_projects()
    path = resume_project_path()
    return load_project(path) if path is not None else None


def project_file_path(filename, root=None):
    if (not isinstance(filename, str) or not filename or any(c in filename for c in '/\\:*?"<>|')
            or any(ord(c) < 32 for c in filename)
            or Path(filename).name != filename or not filename.endswith('.json')):
        raise ValueError('Invalid project filename.')
    return Path(root if root is not None else PROJECTS) / filename


def discover_projects(root=None):
    """Read saved JSON files and display-only metadata; report unreadable files."""
    directory = Path(root if root is not None else PROJECTS)
    warnings = []
    try:
        settings = load_settings(strict=True)
    except (OSError, ValueError):
        settings = {}
        warnings.append('Could not read project display metadata. Settings were left unchanged.')
    display_names = settings.get('project_display_names', {})
    if not isinstance(display_names, dict):
        display_names = {}
        warnings.append('Invalid project display metadata. Settings were left unchanged.')
    saved = {}
    for path in sorted(directory.glob('*.json')) if directory.exists() else []:
        try:
            project = load_project(path)
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
            friendly = display_names.get(path.name, '')
            saved[path.name] = {
                'filename': path.name, 'project': project,
                'title': str(project.get('title') or 'Unnamed project'),
                'display_name': friendly if isinstance(friendly, str) else '',
                'candidate_count': len(project['names']), 'modified_at': modified,
            }
        except (OSError, ValueError, KeyError, TypeError):
            warnings.append(f'Could not read {path.name}. The file has been left unchanged.')
    return saved, warnings


def project_display_label(entry):
    title = entry['title']
    name = entry['display_name'] or title
    original = f" | Project: {title}" if entry['display_name'] else ''
    date = datetime.fromisoformat(entry['modified_at']).strftime('%Y-%m-%d %H:%M UTC')
    return f"{name}{original} | {entry['filename']} | {entry['candidate_count']} candidates | {date}"


def save_project_display_name(filename, display_name):
    """Write friendly names to settings only, never to the project database."""
    path = project_file_path(filename)
    load_project(path)
    if not isinstance(display_name, str):
        raise ValueError('Enter a friendly display name.')
    display_name = display_name.strip()
    if len(display_name) > 120 or any(ord(c) < 32 for c in display_name):
        raise ValueError('Use a single-line friendly name of at most 120 characters.')
    settings = load_settings(strict=True)
    names = settings.get('project_display_names', {})
    if not isinstance(names, dict):
        raise ValueError('Invalid display metadata. Settings have been left unchanged.')
    names = dict(names)
    if display_name:
        names[filename] = display_name
    else:
        names.pop(filename, None)
    settings['project_display_names'] = names
    write_json(DATA / 'settings.json', settings)


def clean_names(names):
    result, seen = [], set()
    for name in names:
        name = str(name).strip()
        if not name:
            continue
        if not normalize(name):
            raise ValueError('A candidate must contain letters or numbers.')
        if len(name) > 120:
            raise ValueError('Candidate names must be 120 characters or fewer.')
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            result.append(name)
    return result


def import_csv(content):
    text = content.decode('utf-8-sig') if isinstance(content, bytes) else content.lstrip('\ufeff')
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        raise ValueError('The file contains no candidates.')
    headers = [c.strip().casefold() for c in rows[0]]
    column = next((i for i, c in enumerate(headers) if c in ('name', 'brand name', 'candidate', 'candidate name', 'channel name')), None)
    if column is not None:
        data = [r[column] for r in rows[1:] if len(r) > column]
    elif all(len(r) == 1 for r in rows):
        data = [r[0] for r in rows]
    else:
        raise ValueError('For multiple columns, include a Name or Candidate column heading.')
    names = clean_names(data)
    if not names:
        raise ValueError('The file contains a heading but no candidate names.')
    return names


def import_pasted(text):
    """Accept plain lists, a copied single-column Excel table, or Markdown tables."""
    names = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith('|'):
            cells = [c.strip() for c in line.strip('|').split('|')]
            if all(re.fullmatch(r'[:\- ]+', c) for c in cells):
                continue
            line = cells[1] if len(cells) > 1 and cells[0].isdigit() else cells[0]
        elif '\t' in line:
            cells = line.split('\t')
            line = cells[1] if cells[0].isdigit() and len(cells) > 1 else cells[0]
        line = re.sub(r'^(?:[-*•]\s+|\d+[.)]\s+)', '', line).strip()
        if line.casefold() in ('name', 'candidate', 'candidate name', 'channel name'):
            continue
        names.append(line)
    return clean_names(names)


def new_project(title='Garden by the Sea'):
    if title != 'Garden by the Sea':
        return Project({'id': uuid.uuid4().hex, 'title': title, 'brief': '', 'audience': '', 'platform': 'Business / other',
                        'names': [], 'reviews': {}, 'runs': [], 'version': VERSION})
    return Project({'id': uuid.uuid4().hex, 'title': title, 'brief': 'A fictional channel about gardening and island games.',
            'audience': 'Players interested in casual games.',
            'platform': 'YouTube / gaming', 'names': [], 'reviews': {}, 'runs': [], 'version': VERSION})


def project_path(project_id, root=None):
    if not re.fullmatch(r'[a-f0-9]{32}', project_id):
        raise ValueError('Invalid project identifier.')
    return Path(root if root is not None else PROJECTS) / (project_id + '.json')


@contextmanager
def project_write_lock(path):
    lock = path.with_suffix(path.suffix + '.lock')
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ProjectConflict('Another save is in progress, or an interrupted save left a lock. '
                              'Keep your unsaved backup and retry after the other session closes.') from None
    try:
        yield
    finally:
        os.close(descriptor)
        lock.unlink()


def save_project(project, root=None, *, filename=None):
    project_path(project['id'], root)
    path = project_file_path(filename, root) if filename is not None else project_path(project['id'], root)
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    expected = project._revision if isinstance(project, Project) and project._path == path else None
    with project_write_lock(path):
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        if actual != expected:
            raise ProjectConflict('This database changed in another session. Your edits were not saved. '
                                  'Download the unsaved backup, then reload the saved database before editing again.')
        saved = dict(project, updated_at=now(), version=VERSION)
        revision = write_json(path, saved)
        project.update(saved)
        if isinstance(project, Project):
            project._path = path
            project._revision = revision


def load_project(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    p = Project(json.loads(raw.decode('utf-8')))
    project_path(p['id'])
    if not isinstance(p.get('names'), list) or not isinstance(p.get('reviews'), dict) or not isinstance(p.get('runs'), list):
        raise ValueError('Invalid project data.')
    p._path = path
    p._revision = hashlib.sha256(raw).hexdigest()
    return p


def rating_score(review):
    values = [review.get(k) for k in RATING_FIELDS]
    if any(type(v) is not int or not 1 <= v <= 5 for v in values):
        return None
    return round(sum(values) / 20 * 100)


def fetch_json(url, params=None):
    try:
        response = requests.get(url, params=params, timeout=(4, 12), headers={'User-Agent': 'BrandDiscoveryEngine/0.4'})
        code = response.status_code
        try:
            data = response.json()
        except ValueError:
            data = None
        if (code == 200 and urlparse(url).hostname == 'serpapi.com' and isinstance(data, dict)
                and data.get('error') == "Google hasn't returned any results for this query."
                and isinstance(data.get('search_metadata'), dict)
                and data['search_metadata'].get('status') == 'Success'
                and ('organic_results' not in data or data['organic_results'] == [])):
            data = {k: v for k, v in data.items() if k != 'error'} | {'organic_results': []}
        if code >= 300 or (isinstance(data, dict) and 'error' in data):
            error = data.get('error', {}) if isinstance(data, dict) else {}
            errors = error.get('errors', []) if isinstance(error, dict) else []
            reasons = [str(x.get('reason', '')) for x in errors if isinstance(x, dict)] if isinstance(errors, list) else []
            error_text = error.casefold() if isinstance(error, str) else ''
            if (any('quota' in x.lower() or 'limit' in x.lower() for x in reasons) or code == 429
                    or any(word in error_text for word in ('quota', 'rate limit', 'run out of searches'))):
                return None, 'Provider quota or rate limit reached. Wait for reset or check your provider quota.'
            if (code in (401, 403) or any(x in ('keyInvalid', 'accessNotConfigured') for x in reasons)
                    or any(word in error_text for word in ('api key', 'unauthorized', 'authentication'))):
                return None, 'API access denied. Check the saved key, API enablement, restrictions and quota in the provider console.'
            return None, f'Provider returned an API error (HTTP {code}). Check the provider console.'
        if not isinstance(data, dict) or 'error' in data:
            return None, 'The provider returned an invalid response or API error.'
        return data, ''
    except requests.Timeout:
        return None, 'Request timed out.'
    except requests.HTTPError as e:
        return None, 'Provider returned HTTP ' + str(e.response.status_code) + '.'
    except (requests.RequestException, ValueError):
        # Never include request URLs: API keys can appear in them.
        return None, 'Could not complete the request. Check the connection and API settings.'


def brand_words(text):
    text = unicodedata.normalize('NFKC', html.unescape(text))
    text = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', text)
    text = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', text)
    return re.findall(r'[^\W_]+', text.casefold())


def contains_brand(name, text):
    words = brand_words(name)
    if not words:
        return False
    pattern = r"(?<!\w)" + r"[\s&'._\-\u2019]*".join(re.escape(word) for word in words) + r"(?!\w)"
    return re.search(pattern, unicodedata.normalize('NFKC', html.unescape(text)).casefold()) is not None


def brand_identity(name, title, url):
    segments = re.split(r'[:|\u2013\u2014]|\s+-\s+', html.unescape(title))
    if any(normalize(segment) == normalize(name) and contains_brand(name, segment) for segment in segments):
        return True
    return any(normalize(label) == normalize(name) for label in (urlparse(url).hostname or '').split('.'))


def classify_match(name, title, description='', url='', handle=''):
    target = normalize(name)
    fields = {'Title': title, 'Description': description, 'URL': unquote(url), 'Handle': handle}
    hits = [field for field, text in fields.items() if contains_brand(name, text)]
    if 'Title' in hits and normalize(title) == target:
        kind = 'Exact title'
    elif 'Title' in hits:
        kind = 'Name in title'
    elif hits:
        kind = 'Name in other fields'
    elif target and SequenceMatcher(None, target, normalize(title)).ratio() >= .78:
        kind = 'Similar title'
    else:
        kind = 'Review relevance'
    return kind, ', '.join(hits)


def canonical_url(url):
    parts = urlparse(url)
    return parts.netloc.casefold().removeprefix('www.') + unquote(parts.path).rstrip('/').casefold()


def search_check(source, name, api_key='', target=''):
    q = quote_plus(name)
    links = {'Apple Podcasts': 'https://podcasts.apple.com/us/search?term=' + q,
             'YouTube': 'https://www.youtube.com/results?search_query=' + q,
             'Google': 'https://www.google.com/search?q=' + quote_plus('"' + name + '"'),
             'Google broad': 'https://www.google.com/search?q=' + q,
             'Industry search': 'https://www.google.com/search?q=' + quote_plus('"' + name + '" ' + target)}
    social = {'Facebook': 'facebook.com', 'TikTok': 'tiktok.com', 'Instagram': 'instagram.com'}
    query = name if source in ('YouTube', 'Apple Podcasts', 'Google broad') else '"' + name + '"'
    if source == 'Industry search':
        terms = [x.strip() for x in target.split(',') if x.strip()][:10]
        query = '"' + name + '" (' + ' OR '.join('"' + x.replace('"', '') + '"' for x in terms) + ')' if terms else '"' + name + '"'
        links[source] = 'https://www.google.com/search?q=' + quote_plus(query)
    if source in social:
        query = f'site:{social[source]} ("{name}" OR "{target or slug(name)}")'
        links[source] = 'https://www.google.com/search?q=' + quote_plus(query)
    result = {'source': source, 'status': 'Not checked', 'url': links[source], 'query': query,
              'matches': [], 'exact_count': None, 'message': ''}
    if source != 'Apple Podcasts' and not api_key:
        result['message'] = 'Open the search link for manual review, or supply an API key.'
        return result
    if source == 'Apple Podcasts':
        data, error = fetch_json('https://itunes.apple.com/search', {'term': name, 'media': 'podcast', 'entity': 'podcast', 'limit': 25})
        items = data.get('results', [] if data.get('resultCount') == 0 else None) if data is not None else None
    elif source == 'YouTube':
        data, error = fetch_json('https://www.googleapis.com/youtube/v3/search', {'part': 'snippet', 'type': 'channel', 'maxResults': 50, 'q': name, 'key': api_key})
        page = data.get('pageInfo') if data is not None else None
        empty = isinstance(page, dict) and page.get('totalResults') == 0
        items = data.get('items', [] if empty else None) if data is not None else None
    else:
        data, error = fetch_json('https://serpapi.com/search.json', {'engine': 'google', 'q': query, 'api_key': api_key})
        metadata = data.get('search_metadata') if data is not None else None
        information = data.get('search_information') if data is not None else None
        empty = (isinstance(metadata, dict) and metadata.get('status') == 'Success'
                 and isinstance(information, dict)
                 and (information.get('organic_results_state') == 'Fully empty' or information.get('total_results') == 0))
        items = data.get('organic_results', [] if empty else None) if data is not None else None
        if metadata is not None and (not isinstance(metadata, dict) or metadata.get('status') != 'Success'):
            items = None
    if data is None or not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        result.update(status='Failed', message=error or 'Unexpected provider response.')
        return result
    details = {}
    if source == 'YouTube':
        if any(not isinstance(x.get('id', {}), dict) or not isinstance(x.get('snippet', {}), dict) for x in items):
            result.update(status='Failed', message='Unexpected YouTube search result.')
            return result
        ids = [x.get('id', {}).get('channelId', '') for x in items]
        ids = [x for x in ids if x]
        if ids:
            extra, extra_error = fetch_json('https://www.googleapis.com/youtube/v3/channels',
                                           {'part': 'snippet', 'id': ','.join(ids), 'key': api_key})
            if (extra is not None and isinstance(extra.get('items'), list)
                    and all(isinstance(x, dict) and isinstance(x.get('snippet', {}), dict) for x in extra['items'])):
                details = {x['id']: x.get('snippet', {}) for x in extra['items'] if isinstance(x.get('id'), str)}
            else:
                result['details_error'] = extra_error or 'Channel details could not be retrieved.'
    seen = set()
    for item in items:
        description, handle = '', ''
        if source == 'Apple Podcasts':
            title, url = item.get('collectionName', ''), item.get('collectionViewUrl', '')
        elif source == 'YouTube':
            channel_id = item.get('id', {}).get('channelId', '')
            snippet = details.get(channel_id, item.get('snippet', {}))
            title = snippet.get('title', '')
            description = snippet.get('description', '')
            handle = snippet.get('customUrl', '')
            url = 'https://www.youtube.com/channel/' + channel_id if channel_id else ''
        else:
            title, url = item.get('title', ''), item.get('link', '')
            description = item.get('snippet', '')
        if not all(isinstance(value, str) for value in (title, url, description, handle)) or not title:
            result.update(status='Failed', matches=[], exact_count=None, message='Unexpected search result fields.')
            return result
        identity = canonical_url(url) if url else normalize(title)
        if title and identity not in seen:
            seen.add(identity)
            kind, fields = classify_match(name, title, description, url, handle)
            result['matches'].append({'title': html.unescape(title), 'description': description, 'url': url,
                                      'handle': handle, 'match': kind, 'matched_fields': fields})
    result.update(status='Completed', exact_count=sum(m['match'] == 'Exact title' for m in result['matches']),
                  message='Limited search sample; review titles and context. No results does not prove availability.')
    return result


def dns_check(name):
    domain = slug(name) + '.com'
    result = {'domain': domain, 'status': 'Unknown', 'message': ''}
    if len(domain.split('.')[0]) > 63:
        result['message'] = 'Name is too long for this domain label.'
        return result
    data, error = fetch_json('https://dns.google/resolve', {'name': domain, 'type': 'A', 'edns_client_subnet': '0.0.0.0/0'})
    if data is None or data.get('Status') not in (0, 3):
        result.update(status='Failed', message=error or 'DNS lookup failed; availability is unknown.')
    elif any(answer.get('type') == 1 for answer in data.get('Answer', [])):
        result.update(status='Resolves', message='An A record was returned. Check registration and relevance separately.')
    else:
        result.update(status='No A record', message='May still be registered or have other DNS records. Confirm at a registrar.')
    return result


def default_targets(name):
    handle = slug(name)
    return {k: handle for k in ('YouTube', 'Gmail', 'Facebook', 'TikTok', 'Instagram')} | {'Domain': handle + '.com'}


def targets_for(project, name):
    return default_targets(name) | project['reviews'].get(name.casefold(), {}).get('targets', {})


def youtube_handle_check(handle, api_key):
    result = {'target': handle, 'status': 'Not checked', 'message': 'Enter a YouTube API key.',
              'url': 'https://www.youtube.com/@' + handle, 'matches': []}
    if not handle:
        result.update(status='Invalid target', message='Enter a proposed YouTube handle.')
    elif api_key:
        data, error = fetch_json('https://www.googleapis.com/youtube/v3/channels',
                                {'part': 'snippet', 'forHandle': handle, 'key': api_key})
        page = data.get('pageInfo') if data is not None else None
        empty = isinstance(page, dict) and page.get('totalResults') == 0
        items = data.get('items', [] if empty else None) if data is not None else None
        valid_items = (isinstance(items, list) and all(
            isinstance(item, dict) and isinstance(item.get('id'), str) and bool(item['id'])
            and isinstance(item.get('snippet'), dict)
            and all(isinstance(item['snippet'].get(key, ''), str) for key in ('title', 'description', 'customUrl'))
            for item in items))
        if data is None or not valid_items:
            result.update(status='Failed', message=error or 'Unexpected provider response.')
        elif items:
            result.update(status='Handle found', message='YouTube returned an existing channel for this handle.')
            for item in items:
                s = item.get('snippet', {})
                result['matches'].append({'title': s.get('title', ''), 'description': s.get('description', ''),
                                          'handle': s.get('customUrl', ''), 'url': 'https://www.youtube.com/channel/' + item['id'],
                                          'match': 'Handle found', 'matched_fields': 'Handle'})
        else:
            result.update(status='No channel returned', message='No channel returned; confirm eligibility in YouTube before claiming this handle.')
    return result


def selected_search_sources(profile):
    sources = ['Google', 'Google broad', 'Industry search']
    if profile['youtube']:
        sources.insert(0, 'YouTube')
    if profile['social']:
        sources += ['Facebook', 'TikTok', 'Instagram']
    if profile['podcasts']:
        sources.append('Apple Podcasts')
    return sources


def completed_check(source, check):
    if not check or check.get('details_error') or check.get('retry_needed'):
        return False
    statuses = {'Domain registration': ('Registered', 'Potentially available'),
                'Domain DNS': ('Resolves', 'No A record'),
                'YouTube handle': ('Handle found', 'No channel returned')}
    return check.get('status') in statuses.get(source, ('Completed',))


def research_context(source, targets, profile):
    if source == 'Industry search':
        return profile.get('terms')
    if source in ('Domain registration', 'Domain DNS'):
        return targets.get('Domain')
    if source == 'YouTube handle':
        return targets.get('YouTube')
    if source in ('Facebook', 'TikTok', 'Instagram'):
        return targets.get(source)
    return None


def research_name(name, youtube_key='', google_key='', podcasts=False, targets=None, social=True, profile=None,
                  *, previous=None, force=False):
    from assessment import default_profile
    from domain_pricing import valid_domain
    profile = profile or default_profile('YouTube / gaming') | {'podcasts': podcasts, 'social': social}
    targets = targets or default_targets(name)
    previous = previous or {}
    prior_checks = {c['source']: c for c in previous.get('checks', [])}

    def collect(source, prior, request):
        old_context = research_context(source, previous.get('targets', default_targets(name)), previous.get('profile', {}))
        if source == 'Industry search':
            old_context = prior.get('profile_terms', old_context)
        context_matches = old_context == research_context(source, targets, profile)
        if not force and context_matches and completed_check(source, prior):
            return deepcopy(prior) | {'checked_at': prior.get('checked_at', previous.get('checked_at', ''))}
        return request() | {'checked_at': now()}

    checks = []
    for source in selected_search_sources(profile):
        key = youtube_key if source == 'YouTube' else google_key
        target = profile['terms'] if source == 'Industry search' else targets.get(source, '')
        checks.append(collect(source, prior_checks.get(source, {}),
                              lambda: search_check(source, name, key, target)))
    domain = targets['Domain'].strip().lower()
    skipped = {'domain': domain, 'status': 'Not checked',
               'message': 'Routine screening requires a .com target; historical data remains saved.'}
    dns = collect('Domain DNS', previous.get('dns', {}), lambda: dns_check_domain(domain)) if valid_domain(domain) else skipped.copy()
    registration = collect('Domain registration', previous.get('registration', {}),
                           lambda: registration_check(domain)) if valid_domain(domain) else skipped.copy()
    return {'name': name, 'checked_at': now(), 'targets': targets,
            'dns': dns, 'registration': registration, 'profile': profile,
            'youtube_handle': collect('YouTube handle', previous.get('youtube_handle', {}),
                                      lambda: youtube_handle_check(targets['YouTube'], youtube_key)) if profile['youtube'] else {'target': targets['YouTube'], 'status': 'Not selected', 'message': 'YouTube is off for this project.', 'matches': []},
            'gmail': {'target': targets['Gmail'], 'status': 'Manual check required',
                      'message': 'Confirm this username in Google account creation. Search results cannot prove Gmail availability.'},
            'checks': checks}


def dns_check_domain(domain):
    result = {'domain': domain, 'status': 'Unknown', 'message': ''}
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+', domain):
        result['message'] = 'Enter a valid domain.'
        return result
    data, error = fetch_json('https://dns.google/resolve', {'name': domain, 'type': 'A', 'edns_client_subnet': '0.0.0.0/0'})
    answers = data.get('Answer', []) if data is not None else None
    if (data is None or data.get('Status') not in (0, 3)
            or not isinstance(answers, list) or not all(isinstance(a, dict) for a in answers)):
        result.update(status='Failed', message=error or 'DNS lookup failed; availability is unknown.')
    elif any(a.get('type') == 1 for a in answers):
        result.update(status='Resolves', message='An A record was returned. Check registration and relevance separately.')
    else:
        result.update(status='No A record', message='May still be registered. Confirm at a registrar.')
    return result


def registration_check(domain):
    """Registry RDAP, independently of DNS. A missing record is not a purchase guarantee."""
    result = {'domain': domain, 'status': 'Check failed', 'message': '', 'url': 'https://lookup.icann.org/en/lookup?name=' + quote_plus(domain)}
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+', domain):
        result['message'] = 'Invalid domain; enter a domain without https:// or a path.'
        return result
    tld = domain.rsplit('.', 1)[-1]
    base = {'com': 'https://rdap.verisign.com/com/v1/', 'net': 'https://rdap.verisign.com/net/v1/'}.get(tld)
    if not base:
        bootstrap, error = fetch_json('https://data.iana.org/rdap/dns.json')
        if bootstrap is not None:
            services = bootstrap.get('services')
            valid = isinstance(services, list) and all(
                isinstance(service, list) and len(service) == 2
                and all(isinstance(values, list) and all(isinstance(value, str) for value in values) for values in service)
                for service in services)
            if not valid:
                result['message'] = 'Registry bootstrap returned a malformed response. Availability is unknown.'
                return result
            base = next((url for endings, urls in services if tld in endings for url in urls if url.startswith('https://')), None)
        if not base:
            result['message'] = error or 'No registry RDAP endpoint found for this extension. Confirm at a registrar.'
            return result
    result['url'] = base.rstrip('/') + '/domain/' + domain
    try:
        response = requests.get(result['url'], timeout=(4, 12), headers={'Accept': 'application/rdap+json', 'User-Agent': 'BrandDiscoveryEngine/0.6'},
                                allow_redirects=False)
        result['http_status'] = response.status_code
        try:
            data = response.json()
        except ValueError:
            data = None
        empty_registry_404 = (data is None and response.content == b''
                              and urlparse(result['url']).hostname == 'rdap.verisign.com')
        if response.status_code == 404 and ((isinstance(data, dict) and data.get('errorCode') == 404) or empty_registry_404):
            result.update(status='Potentially available', message='Registry returned no domain record. Confirm sale eligibility, reservations and price at a registrar.')
        elif (response.status_code == 200 and isinstance(data, dict) and data.get('objectClassName') == 'domain'
              and isinstance(data.get('ldhName'), str) and data['ldhName'].casefold() == domain.casefold()):
            result.update(status='Registered', message='Registry returned a matching registered domain. You can edit the proposed domain and research an alternative.', registry_status=data.get('status', []))
        elif response.status_code == 429:
            result['message'] = 'Registry rate limit reached. Retry later; this is not an availability result.'
        else:
            result['message'] = 'Registry returned an unexpected response (HTTP ' + str(response.status_code) + '). Availability is unknown.'
    except requests.Timeout:
        result['message'] = 'Registry lookup timed out. Availability is unknown.'
    except (requests.RequestException, ValueError):
        result['message'] = 'Could not complete the registry lookup. Availability is unknown.'
    return result


def research_summary(row):
    checks = row['checks']
    if any(c['status'] == 'Completed' and any(classify_match(row['name'], m.get('title', ''))[0] == 'Exact title' for m in c.get('matches', [])) for c in checks):
        return 'Exact title found — review context'
    if (any(c['status'] != 'Completed' or c.get('retry_needed') for c in checks)
            or row.get('dns', {}).get('status') not in ('No A record', 'Resolves')
            or row.get('refresh_failures')):
        return 'Research incomplete'
    return 'Search sample reviewed — confirm manually'


def search_links(name):
    q, domain = quote_plus(name), slug(name) + '.com'
    return {'YouTube': 'https://www.youtube.com/results?search_query=' + q,
            'Google exact': 'https://www.google.com/search?q=' + quote_plus('"' + name + '"'),
            'Namecheap': 'https://www.namecheap.com/domains/registration/results/?domain=' + domain,
            'Instagram': 'https://www.instagram.com/' + normalize(name) + '/',
            'TikTok': 'https://www.tiktok.com/search?q=' + q,
            'Facebook': 'https://www.facebook.com/search/top?q=' + q,
            'Gmail signup': 'https://accounts.google.com/signup',
            'USPTO search': 'https://tmsearch.uspto.gov/'}


def candidate_links(project, name):
    """One explicit destination per selected platform; use saved targets."""
    from assessment import profile_for
    profile = profile_for(project)
    target = targets_for(project, name)
    links = {'Search name on Google': 'https://www.google.com/search?q=' + quote_plus('"' + name + '"'),
             'Check domain at registrar': 'https://www.namecheap.com/domains/registration/results/?domain=' + quote_plus(target['Domain']),
             'Trademark search (USPTO)': 'https://tmsearch.uspto.gov/'}
    if profile['youtube']:
        links['YouTube — proposed handle'] = 'https://www.youtube.com/@' + quote_plus(target['YouTube'])
    if profile['gmail']:
        links['Gmail — check at signup'] = 'https://accounts.google.com/signup'
    if profile['social']:
        links.update({'Facebook — proposed profile': 'https://www.facebook.com/' + quote_plus(target['Facebook']),
                      'TikTok — proposed profile': 'https://www.tiktok.com/@' + quote_plus(target['TikTok']),
                      'Instagram — proposed profile': 'https://www.instagram.com/' + quote_plus(target['Instagram']) + '/'})
    if profile['podcasts']:
        links['Search Apple Podcasts'] = 'https://podcasts.apple.com/us/search?term=' + quote_plus(name)
    return links


def sync_automatic_decisions(project, assessed_rows):
    """Compatibility entry point: screening must never rewrite saved decisions."""
    return False


def export_rows(project):
    from domain_pricing import pricing_fields
    latest = latest_results(project)
    rows = []
    for name in project['names']:
        review = project['reviews'].get(name.casefold(), {})
        row = latest.get(name.casefold())
        rows.append({'Name': name, 'Brand fit score': rating_score(review), 'Decision': review.get('decision', 'Undecided'),
                     'Notes': review.get('notes', ''), 'Domain review': review.get('domain', 'Unknown'),
                     'Social review': review.get('social', 'Unknown'), 'Trademark review': review.get('trademark', 'Unknown'),
                     'Research': research_summary(row) if row else 'Not checked in latest run',
                     'Checked at': row['checked_at'] if row else '', **{k: review.get(k) for k in RATING_FIELDS},
                     **search_links(name), **ranking_fields(project, name, row),
                     **pricing_fields(project, name, research_row=row)})
    rows.sort(key=lambda r: ({'Shortlist': 0, 'Investigate': 1, 'Reject': 2}[r['Recommendation']],
                             -r['Universal score'], -r['Brand priority points'], -r['Project points'],
                             -(r['Brand fit score'] or 0), r['Name'].casefold()))
    for i, row in enumerate(rows, 1):
        row['Rank'] = i
    return rows


def csv_bytes(project):
    rows = export_rows(project)
    if not rows:
        raise ValueError('Add at least one candidate before exporting.')
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    # Prevent spreadsheet formulas from candidate names or notes.
    for row in rows:
        prepared = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in row.items()}
        writer.writerow({k: ("'" + v if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@')) else v) for k, v in prepared.items()})
    return output.getvalue().encode('utf-8-sig')


PLATFORMS = ('YouTube', 'Gmail', 'Facebook', 'TikTok', 'Instagram', 'Domain')
AVAILABILITY = ['Unknown', 'Needs review', 'Confirmed available', 'Owned by me', 'Conflict found']


def latest_results(project):
    from assessment import profile_for
    profile = profile_for(project)
    active_sources = selected_search_sources(profile) + ['Domain DNS', 'Domain registration']
    if profile['youtube']:
        active_sources.append('YouTube handle')
    fields = {'Domain DNS': 'dns', 'Domain registration': 'registration', 'YouTube handle': 'youtube_handle'}
    result = {}
    attempts, successes = {}, {}
    for run in project['runs']:
        for row in run.get('results', []):
            name = row['name']
            key = name.casefold()
            result[key] = dict(row)
            targets = targets_for(project, name)
            source_records = {c['source']: c for c in row.get('checks', [])}
            source_records.update({source: row[field] for source, field in fields.items() if row.get(field)})
            for source, record in source_records.items():
                if source not in active_sources:
                    continue
                context = research_context(source, row.get('targets', default_targets(name)), row.get('profile', {}))
                if context != research_context(source, targets, profile):
                    continue
                record = deepcopy(record)
                record.setdefault('checked_at', row.get('checked_at', ''))
                if source == 'Industry search':
                    record['profile_terms'] = context
                attempts.setdefault(key, {})[source] = record
                if completed_check(source, record):
                    successes.setdefault(key, {})[source] = record
    for key, row in result.items():
        row['checks'] = []
        row['refresh_failures'] = []
        row['targets'] = targets_for(project, row['name'])
        for field in fields.values():
            row.pop(field, None)
        for source, attempt in attempts.get(key, {}).items():
            success = successes.get(key, {}).get(source)
            check = deepcopy(success if success is not None else attempt)
            if not completed_check(source, attempt):
                check['retry_needed'] = True
                failure = {'source': source, 'checked_at': attempt.get('checked_at', ''),
                           'status': attempt.get('status', 'Unknown'),
                           'message': attempt.get('details_error') or attempt.get('message', 'Incomplete check.'),
                           'evidence_checked_at': success.get('checked_at', '') if success else ''}
                check['last_attempt'] = failure
                row['refresh_failures'].append(failure)
            if source in fields:
                row[fields[source]] = check
            else:
                row['checks'].append(check)
    return result


def effective_status(project, name, platform, row):
    targets = targets_for(project, name)
    review = project['reviews'].get(name.casefold(), {})
    manual = review.get('availability', {}).get(platform, {})
    if platform == 'Gmail':
        value = targets[platform]
        valid = re.fullmatch(r'[a-z0-9]+(?:\.[a-z0-9]+)*', value) and 6 <= len(value.replace('.', '')) <= 30
        if not valid:
            return 'Invalid Gmail username', 'Use 6–30 letters or numbers; periods cannot start, end or repeat.', ''
    if manual.get('target') == targets[platform] and manual.get('status') not in ('Unknown', None):
        return manual['status'], 'Manual review: ' + manual.get('evidence', ''), manual.get('checked_at', '')
    if platform == 'Gmail':
        return 'Manual check required', 'Use Google signup to check this exact username.', ''
    if not row:
        return 'Unknown', 'No saved research for this candidate.', ''
    historical_targets = row.get('targets', default_targets(name))
    if historical_targets.get(platform) != targets[platform]:
        return 'Unknown', 'Target changed; run research again.', ''
    checks = {c['source']: c for c in row.get('checks', [])}
    if platform == 'YouTube':
        handle = row.get('youtube_handle', {})
        check = checks.get('YouTube', {})
        handle_date = handle.get('checked_at', row['checked_at'])
        search_date = check.get('checked_at', row['checked_at'])
        combined_date = min(handle_date, search_date)
        if handle.get('status') == 'Handle found':
            return 'Conflict found', 'The proposed YouTube handle belongs to an existing channel.', handle_date
        matches = check.get('matches', [])
        if any(classify_match(name, m.get('title', ''), m.get('description', ''), m.get('url', ''), m.get('handle', ''))[0] == 'Exact title' for m in matches):
            return 'Name conflict found', 'Exact channel title found; titles are not unique, so review the competing channel.', search_date
        if handle.get('status') == 'No channel returned' and check.get('status') == 'Completed':
            if any(classify_match(name, m.get('title', ''), m.get('description', ''), m.get('url', ''), m.get('handle', ''))[0] in ('Name in title', 'Similar title') for m in matches):
                return 'Needs review', 'No channel at this handle, but similar channel names appear in the sample.', combined_date
            return 'No match in sample', 'No exact title or handle returned; claim eligibility remains unverified.', combined_date
        return 'Unknown', 'YouTube title search or handle lookup is incomplete.', combined_date
    if platform == 'Domain':
        if row.get('registration'):
            record = row['registration']
            return record['status'], record['message'], record.get('checked_at', row['checked_at'])
        record = row.get('dns', {})
        return record.get('status', 'Unknown'), record.get('message', 'No current registry lookup.'), record.get('checked_at', row['checked_at'])
    check = checks.get(platform, {})
    checked_at = check.get('checked_at', row['checked_at'])
    if check.get('status') != 'Completed':
        return 'Unknown', check.get('message', 'Platform search not run.'), checked_at
    domain = {'Facebook': 'facebook.com', 'TikTok': 'tiktok.com', 'Instagram': 'instagram.com'}[platform]
    wanted = domain + '/' + ('@' if platform == 'TikTok' else '') + targets[platform].casefold()
    if any(canonical_url(m.get('url', '')) == wanted for m in check.get('matches', [])):
        return 'Indexed profile found', 'An indexed page uses this exact profile URL; confirm it is current.', checked_at
    if any(classify_match(name, m.get('title', ''), m.get('description', ''), m.get('url', ''))[0] != 'Review relevance' for m in check.get('matches', [])):
        return 'Needs review', 'Potential name references appear in indexed search results.', checked_at
    return 'No match in sample', 'No indexed match in the sample; platform signup must confirm availability.', checked_at


def ranking_fields(project, name, row):
    from assessment import assess
    return assess(project, name, row)


def save_review(project, name, review):
    old = project['reviews'].get(name.casefold(), {})
    old_targets = default_targets(name) | old.get('targets', {})
    changed = []
    for platform, target in review.get('targets', {}).items():
        if target != old_targets[platform]:
            changed.append(platform)
            review['availability'][platform] = {'status': 'Unknown', 'target': target, 'checked_at': '', 'evidence': ''}
    project['reviews'][name.casefold()] = old | review
    return changed


def merge_candidate_csv(project, content):
    names = import_csv(content)
    project['names'] = clean_names(project['names'] + names)
    rows = list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))
    for raw in rows:
        row = {str(k).strip().casefold(): (v or '').strip() for k, v in raw.items() if k is not None and isinstance(v, str)}
        name = next((row[k] for k in ('name', 'brand name', 'candidate', 'candidate name', 'channel name') if row.get(k)), None)
        if not name:
            continue
        updates = {}
        for platform, headings in [('Domain', ('domain candidate', 'domain')), ('YouTube', ('youtube handle candidate', 'youtube handle'))]:
            value = next((row[k] for k in headings if row.get(k)), '')
            if value:
                updates[platform] = value.lower().removeprefix('@')
        if updates:
            old = project['reviews'].get(name.casefold(), {})
            review = dict(old)
            review['targets'] = targets_for(project, name) | updates
            review['availability'] = dict(old.get('availability', {}))
            save_review(project, name, review)
    return project['names']


def workbook_data(project):
    """Flat, key-free data for the full workbook, including all saved runs."""
    ranks = export_rows(project)
    leading = ['Rank', 'Name', 'Universal score', 'Brand priority points', 'Brand priority basis',
               'Automatic recommendation', 'Recommendation', 'Recommendation reason', 'Domain status', 'Domain eligibility', 'Conflict assessment',
               'Competing names', 'Ranking confidence', 'Incomplete checks', 'Project points', 'Priority score', 'YouTube status', 'Gmail status',
               'Facebook status', 'TikTok status', 'Instagram status', 'Domain status', 'Next action', 'Ranking reason',
               'Brand fit score', 'Decision']
    columns = list(dict.fromkeys(leading + [k for k in ranks[0] if k not in leading and k != 'Conflict evidence'])) if ranks else list(dict.fromkeys(leading))
    checks, evidence, reviews = [], [], []
    for run in project['runs']:
        for row in run.get('results', []):
            base = {'Run': run.get('id', ''), 'Candidate': row['name'], 'Checked at': row.get('checked_at', '')}
            for check in row.get('checks', []):
                matches = check.get('matches', [])
                classified = [classify_match(row['name'], m.get('title', ''), m.get('description', ''), m.get('url', ''), m.get('handle', ''))[0] for m in matches]
                checks.append(base | {'Source': check['source'], 'Target / query': check.get('query', row['name']),
                                      'Status': check['status'], 'Returned matches': len(matches),
                                      'Exact title matches': classified.count('Exact title') if check['status'] == 'Completed' else None,
                                      'Message': check.get('message', ''), 'Detail error': check.get('details_error', ''), 'Search URL': check['url']})
                for m in matches:
                    kind, fields = classify_match(row['name'], m.get('title', ''), m.get('description', ''), m.get('url', ''), m.get('handle', ''))
                    evidence.append(base | {'Source': check['source'], 'Title': m.get('title', ''),
                                            'Description': m.get('description', ''), 'Handle': m.get('handle', ''),
                                            'Match': kind, 'Matched fields': fields, 'URL': m.get('url', ''), 'Original match': m.get('match', '')})
            for source, key in [('YouTube handle', 'youtube_handle'), ('Gmail', 'gmail'), ('Domain DNS', 'dns'), ('Domain registration', 'registration')]:
                check = row.get(key)
                if check:
                    checks.append(base | {'Source': source, 'Target / query': check.get('target', check.get('domain', '')),
                                          'Status': check['status'], 'Returned matches': len(check.get('matches', [])),
                                          'Exact title matches': None, 'Message': check.get('message', ''), 'Detail error': '', 'Search URL': check.get('url', '')})
                    for m in check.get('matches', []):
                        evidence.append(base | {'Source': source, 'Title': m.get('title', ''), 'Description': m.get('description', ''),
                                                'Handle': m.get('handle', ''), 'Match': m.get('match', ''), 'Matched fields': m.get('matched_fields', ''),
                                                'URL': m.get('url', ''), 'Original match': m.get('match', '')})
    for name in project['names']:
        review = project['reviews'].get(name.casefold(), {})
        for platform in PLATFORMS:
            manual = review.get('availability', {}).get(platform, {})
            reviews.append({'Candidate': name, 'Platform': platform, 'Target': targets_for(project, name)[platform],
                            'Manual status': manual.get('status', 'Unknown'), 'Evidence / notes': manual.get('evidence', ''),
                            'Reviewed at': manual.get('checked_at', ''), 'General notes': review.get('notes', ''),
                            'Decision': review.get('decision', 'Undecided'), 'Conflict assessment override': review.get('conflict_assessment', ''),
                            'Conflict assessment evidence': review.get('conflict_evidence', ''), **{k: review.get(k) for k in RATING_FIELDS}})
    from assessment import profile_for
    method = [
        {'Item': 'Project', 'Details': project['title']},
        {'Item': 'Brief', 'Details': project.get('brief', '')},
        {'Item': 'Audience', 'Details': project.get('audience', '')},
        {'Item': 'Project profile', 'Details': json.dumps(profile_for(project), ensure_ascii=False)},
        {'Item': 'Exported at (UTC)', 'Details': now()},
        {'Item': 'Ranking order', 'Details': 'Screening recommendation first, then universal evidence score, business-name priority, optional project checks, brand-fit score, and name. Priority score is the legacy universal-plus-project total, not the sort key. Rank is a snapshot.'},
        {'Item': 'Universal score (80)', 'Details': 'Domain: fresh verified eligible 30, provisional promising 20, unresolved but potentially available 10, registered or over budget 0. Conflicts: little evidence 35, possible overlap 15, strong or insufficient evidence 0. Coverage: completed required checks times 15.'},
        {'Item': 'Business-name priority', 'Details': 'Four completed user ratings determine up to 20 points. Otherwise length, simple formatting and a non-generic token each contribute up to 5, for up to 15. Prospecting/research terms are displayed but not scored. These surface signals are proxies, not evidence of suitability, memorability or trademark distinctiveness.'},
        {'Item': 'Project score (up to 20)', 'Details': 'Selected YouTube 8 confirmed or 4 no-match; Gmail 4 confirmed; each selected social platform 1 confirmed or 0.5 no-match; Apple Podcasts 5 for a completed sample without title overlap. Core score always sorts ahead of these points.'},
        {'Item': 'Conflict rules', 'Details': 'A completed search, matching business identity, relevant topic terms and a linked brand homepage, channel, app or podcast listing produce a strong conflict lead and automatic Reject. Other matching evidence is a possible overlap. Manual conflict overrides change the screening recommendation but never erase the automatic one or saved decisions. Leads are not legal conclusions.'},
        {'Item': 'Completeness', 'Details': 'Required: exact Google, broad Google, industry Google, domain registry; plus selected YouTube title/handle and Apple Podcast checks. Missing keys, errors and stale industry/target checks lower confidence. Optional social failures remain visible in Checks.'},
        {'Item': 'Domain', 'Details': 'RDAP registry lookup is separate from DNS. Registered means a matching registry domain record. Potentially available means a structured registry 404, not a sale guarantee; verify eligibility, reservations and price at a registrar.'},
        {'Item': 'Old runs', 'Details': 'Older data is retained and reclassified. Run research again for registration records and project industry searches; fields never collected cannot be reconstructed.'},
        {'Item': 'Evidence and limitations', 'Details': 'All saved runs and stored results are exported. Search samples are not exhaustive. Shared words, indexed mentions and brand-page heuristics can produce false positives; source links and manual overrides are available.'},
        {'Item': 'Secrets', 'Details': 'API keys use the operating system credential store and are excluded from exports and backups.'},
        {'Item': 'Registry source', 'Details': 'https://www.verisign.com/news-insights/registration-data-access-protocol/help/'},
        {'Item': 'Registry discovery', 'Details': 'https://data.iana.org/rdap/dns.json'},
    ]
    def sheet(name, data, columns):
        return {'name': name, 'columns': columns, 'rows': [[r.get(k) for k in columns] for r in data]}
    return [sheet('Ranking', ranks, columns),
            sheet('Checks', checks, ['Run', 'Candidate', 'Checked at', 'Source', 'Target / query', 'Status', 'Returned matches', 'Exact title matches', 'Message', 'Detail error', 'Search URL']),
            sheet('Evidence', evidence, ['Run', 'Candidate', 'Checked at', 'Source', 'Title', 'Description', 'Handle', 'Match', 'Matched fields', 'URL', 'Original match']),
            sheet('Reviews', reviews, ['Candidate', 'Platform', 'Target', 'Manual status', 'Evidence / notes', 'Reviewed at', 'General notes', 'Decision', 'Conflict assessment override', 'Conflict assessment evidence', *RATING_FIELDS]),
            sheet('Method', method, ['Item', 'Details'])]
