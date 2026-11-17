import json
import csv
import io
import hashlib
import os
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import streamlit as st

from engine import (VERSION, PROJECTS, RATING_FIELDS, DECISIONS, new_project, save_project,
                    load_project, clean_names, import_csv, now, research_name,
                    research_summary, rating_score, export_rows, csv_bytes, search_links,
                    Project, ProjectConflict)
from engine import (save_settings, stored_key, remember_keys, PLATFORMS,
                    AVAILABILITY, targets_for, default_targets, save_review, workbook_data, import_pasted, classify_match)
from workbook_export import excel_bytes
from assessment import profile_for, default_profile, CONFLICT_OPTIONS
from engine import merge_candidate_csv, candidate_links, latest_results
from research_queue import plan_research, research_state
from domain_pricing import (AFFORDABLE, PROVISIONAL, RESULT_COLUMNS, RESULT_FILTERS, PricingError, budget_for,
                            cached_pricing_rows, check_prices, filter_pricing_rows, observed_quote,
                            plan_prices, pricing_fields, pricing_results_csv, save_quote)
from godaddy_pricing import GoDaddyPricing
from engine import (discover_projects, project_display_label, project_file_path, resume_project_path,
                    save_project_display_name, migrate_local_projects)
from workflow import render_workflow

PRICE_COLUMNS = {
    'Registration price (USD)': st.column_config.NumberColumn('Registration/base price (USD)', format='dollar'),
    'Premium/resale purchase fee (USD)': st.column_config.NumberColumn(format='dollar'),
    'Acquisition price (USD)': st.column_config.NumberColumn('Total quoted acquisition (USD)', format='dollar'),
    'Annual renewal price (USD)': st.column_config.NumberColumn(format='dollar'),
    'Known acquisition minimum (USD)': st.column_config.NumberColumn(format='dollar'),
}

st.set_page_config(page_title='Brand Discovery Engine', layout='wide')
st.title('Brand Discovery Engine')
st.caption(f'Version {VERSION} · Find a name that fits. Review the evidence before you choose.')
st.caption('Saved projects and friendly names are in the sidebar. On a narrow screen, tap the top-left arrow to open it.')


def persist():
    try:
        save_project(st.session_state.project, filename=st.session_state.project_filename)
    except (OSError, ValueError) as exc:
        message = ('Could not save the project: ' + str(exc) if isinstance(exc, ProjectConflict)
                   else 'Could not save the project. Check folder permissions and free space. Your edits remain open and unsaved.')
        st.session_state.save_error = message
        st.error(message)
        return False
    st.session_state.project_saved_snapshot = deepcopy(st.session_state.project)
    st.session_state.pop('save_error', None)
    try:
        save_settings(last_project=st.session_state.project['id'],
                      last_project_filename=st.session_state.project_filename)
    except (OSError, ValueError):
        st.session_state.settings_save_warning = 'Project saved, but last-open settings could not be saved. Check settings metadata and folder permissions.'
        st.warning(st.session_state.settings_save_warning)
    else:
        st.session_state.pop('settings_save_warning', None)
    return True


def project_key(base):
    return base + '_' + st.session_state.project['id']


def activate(p, filename=None):
    st.session_state.pop('save_error', None)
    st.session_state.pop('settings_save_warning', None)
    st.session_state.project = p
    st.session_state.project_filename = filename or p['id'] + '.json'
    st.session_state.project_saved_snapshot = deepcopy(p)
    st.session_state.names_editor = '\n'.join(p['names'])
    st.session_state.pop(project_key('domain_budget'), None)
    profile = profile_for(p)
    values = {'title_editor': p['title'], 'brief_editor': p.get('brief', ''),
              'audience_editor': p.get('audience', ''), 'platform_editor': p.get('platform', 'Business / other'),
              'profile_terms': profile['terms'], 'profile_youtube': profile['youtube'],
              'profile_gmail': profile['gmail'], 'profile_social': profile['social'],
              'profile_podcasts': profile['podcasts']}
    for base, value in values.items():
        st.session_state[project_key(base)] = value


def unsaved_project_changes():
    p = st.session_state.project
    if p != st.session_state.get('project_saved_snapshot', p):
        return True
    if st.session_state.get('names_editor', '\n'.join(p['names'])) != '\n'.join(p['names']):
        return True
    profile = profile_for(p)
    expected = {'title_editor': p.get('title', ''), 'brief_editor': p.get('brief', ''),
                'audience_editor': p.get('audience', ''), 'platform_editor': p.get('platform', 'Business / other'),
                'profile_terms': profile['terms'], 'profile_youtube': profile['youtube'],
                'profile_gmail': profile['gmail'], 'profile_social': profile['social'],
                'profile_podcasts': profile['podcasts']}
    return any(st.session_state.get(project_key(base), value) != value for base, value in expected.items())


def request_project_switch(project, filename=None, create=False):
    target_filename = filename or project['id'] + '.json'
    confirmation_key = 'confirm_project_switch_' + st.session_state.project_filename + '_' + target_filename
    st.session_state[confirmation_key] = False
    st.session_state.pending_project_switch = {
        'project': project, 'filename': target_filename, 'create': create}


def autosave_names():
    try:
        st.session_state.project['names'] = clean_names(st.session_state.names_editor.splitlines())
        return persist()
    except ValueError as e:
        st.error(str(e))
        return False


def save_keys():
    try:
        remember_keys(st.session_state.youtube_key, st.session_state.google_key)
        st.session_state.key_notice = 'Keys saved in this computer’s credential store.'
    except Exception:
        st.session_state.key_notice = 'Could not remember keys on this computer. They still work for this session; install the launcher dependencies and try again.'


if 'project' not in st.session_state:
    migrate_local_projects()
    resumed_path = resume_project_path()
    p = load_project(resumed_path) if resumed_path is not None else None
    if p is None:
        p = new_project('Sample business project')
        p['brief'] = 'A fictional business software project for demonstrating brand research.'
        p['audience'] = 'Professionals interested in business software.'
        p['platform'] = 'Business / other'
        p['research_profile'] = {'youtube': False, 'gmail': False, 'social': False,
                                 'podcasts': False, 'terms': 'business, software, research',
                                 'secondary': 'Domain / business'}
        p['names'] = ['HarborGlyph', 'TideCanvas', 'FieldLoom', 'SailQuill', 'HarborSketch']
    activate(p, resumed_path.name if resumed_path is not None else None)
    if resumed_path is None:
        persist()

st.session_state.setdefault('project_filename', st.session_state.project['id'] + '.json')
st.session_state.setdefault('project_saved_snapshot', deepcopy(st.session_state.project))

if 'pending_names_editor' in st.session_state:
    st.session_state.names_editor = st.session_state.pop('pending_names_editor')
if 'names_added' in st.session_state:
    st.success(f"Added and saved {st.session_state.pop('names_added')} new names.")
if 'saved_notice' in st.session_state:
    st.success(st.session_state.pop('saved_notice'))
if 'target_notice' in st.session_state:
    st.info(st.session_state.pop('target_notice'))
if 'pricing_warning' in st.session_state:
    st.warning(st.session_state.pop('pricing_warning'))
if st.session_state.get('settings_save_warning'):
    st.warning(st.session_state.settings_save_warning)
if st.session_state.get('save_error'):
    st.error(st.session_state.save_error)
    st.download_button('Download unsaved project backup',
                       json.dumps(st.session_state.project, ensure_ascii=False, indent=2).encode('utf-8'),
                       'unsaved-project-backup.json', 'application/json', width='stretch')
    st.caption('This backup contains the current in-memory project, including research and edits that could not be saved. Unsubmitted form edits are not included.')
    if st.button('Retry project save', width='stretch'):
        if persist():
            st.rerun()
    if st.button('Reload saved database', width='stretch'):
        request_project_switch(st.session_state.project, st.session_state.project_filename)

with st.sidebar:
    st.header('Projects')
    saved, discovery_warnings = discover_projects()
    for warning in discovery_warnings:
        st.warning(warning)
    active_filename = st.session_state.project_filename
    active_entry = saved.get(active_filename)
    active_name = (active_entry['display_name'] or st.session_state.project['title']) if active_entry else st.session_state.project['title']
    st.info(f"Active database: {active_name} | {active_filename} | {len(st.session_state.project['names'])} candidates")
    if unsaved_project_changes():
        st.warning('The active project has unsaved changes. Save them before switching, or explicitly discard them.')
    selected = st.selectbox('Saved projects', [''] + list(saved),
                            format_func=lambda v: project_display_label(saved[v]) if v else 'Choose a project database',
                            key='project_selector')
    if selected:
        entry = saved[selected]
        st.caption('Selected database: ' + project_display_label(entry))
        with st.form('friendly_project_name_' + selected):
            friendly = st.text_input('Friendly display name', value=entry['display_name'], max_chars=120,
                                      help='Display metadata only. Blank uses the existing project name; filenames and records stay unchanged.')
            if st.form_submit_button('Save display name', width='stretch'):
                try:
                    save_project_display_name(selected, friendly)
                    st.session_state.saved_notice = 'Friendly display name saved. Database filename and records are unchanged.'
                    st.rerun()
                except (OSError, ValueError, KeyError, TypeError):
                    st.error('Could not save the display name. Check file permissions and settings metadata; project records were not changed.')
    if st.button('Load selected project', disabled=not selected or selected == active_filename, width='stretch'):
        request_project_switch(saved[selected]['project'], selected)
    if st.button('New project', width='stretch'):
        request_project_switch(new_project('New brand project'), create=True)
    st.caption('Candidate edits save when you leave the text box. Your last project reopens automatically.')
    backup = st.file_uploader('Restore project backup (.json)', type=['json'])
    if st.button('Restore as a new project', disabled=backup is None, width='stretch'):
        try:
            restored = json.loads(backup.getvalue().decode('utf-8-sig'))
            if not isinstance(restored, dict) or not all(k in restored for k in ('title', 'names', 'reviews', 'runs')):
                raise ValueError('Invalid backup.')
            restored['names'] = clean_names(restored['names'])
            if not isinstance(restored['reviews'], dict) or not isinstance(restored['runs'], list):
                raise ValueError('Invalid backup.')
            restored['id'] = new_project()['id']
            request_project_switch(Project(restored), create=True)
        except (ValueError, TypeError, KeyError):
            st.error('This file is not a valid Brand Discovery Engine project backup.')
    pending = st.session_state.get('pending_project_switch')
    if pending:
        st.warning('Switch pending: ' + pending['filename'] +
                   '. Save unfinished project, review or listing forms before confirming. '
                   'Unsubmitted form edits are not available to the server and will be lost on switching.')
        confirmed = st.checkbox('I confirm switching databases and discarding any remaining unsaved edits',
                                 key='confirm_project_switch_' + active_filename + '_' + pending['filename'])
        if st.button('Confirm database switch', disabled=not confirmed, width='stretch'):
            try:
                destination = (pending['project'] if pending['create'] and not pending.get('created')
                               else load_project(project_file_path(pending['filename'])))
                if pending['create'] and not pending.get('created'):
                    if project_file_path(pending['filename']).exists():
                        raise ValueError('Destination already exists; will not overwrite it.')
                    save_project(destination, filename=pending['filename'])
                    pending['created'] = True
                save_settings(last_project=destination['id'], last_project_filename=pending['filename'])
                activate(destination, pending['filename'])
                st.session_state.pop('pending_project_switch', None)
                st.rerun()
            except (OSError, ValueError, KeyError, TypeError):
                st.error('Could not switch databases. The active project and its unsaved edits remain open; no existing database was overwritten.')
                if pending.get('created'):
                    st.warning('The requested new database was saved, but switching did not finish. Retry confirmation to open it without rewriting it, or cancel and load it later.')
        if st.button('Cancel database switch', width='stretch'):
            st.session_state.pop('pending_project_switch', None)
            st.rerun()
    st.divider()
    st.caption('Every project ranks domain status, competing-name evidence and research completeness first. Project-specific checks refine the shortlist.')

p = st.session_state.project
active_entry = saved.get(st.session_state.project_filename)
display_name = (active_entry['display_name'] or p['title']) if active_entry else p['title']
st.caption(f"Active project: {display_name} | {st.session_state.project_filename} | {len(p['names'])} candidates")
render_workflow(p, persist, project_key)

with st.expander('Research, pricing and advanced tools'):
    st.subheader('Brand screening · Quick overview')
    st.caption('Review a name, make a decision, and move on. Open detailed evidence only when needed.')

    with st.expander('Domain pricing (read-only)', expanded=True):
        budget = st.number_input('Maximum domain acquisition price (USD)', min_value=0.0,
                                 max_value=100.0, value=float(budget_for(p)), step=1.0,
                                 key=project_key('domain_budget'))
        if budget != budget_for(p):
            p['domain_budget'] = budget
            persist()
        st.caption('Default $100; the firm ceiling cannot be raised. Availability alone is not a price. '
                   'GoDaddy quotes exclude ICANN fees and taxes. Under-budget quotes are Provisional, not Affordable, '
                   'until the final all-in total is independently verified.')
        shortlisted_prices = st.checkbox('Check only shortlisted names (Keep + Investigate)', value=True,
                                          key=project_key('pricing_shortlisted'))
        refresh_prices = st.checkbox('Refresh cached prices', value=False, key=project_key('pricing_refresh'))
        pricing_domains, pending_prices, invalid_prices = plan_prices(
            p, shortlisted_prices, refresh_prices, provider='GoDaddy')
        st.caption(f'{len(pending_prices)} domains will be checked; '
                   f'{len(pricing_domains) - len(pending_prices)} cached results reused. '
                   'Saved quotes and failed attempts are reused for 24 hours unless explicitly refreshed; stale quotes require verification.')
        st.caption('User-observed listings are authoritative and are never overwritten by API checks. '
                   'Reverify/update them using the listing form below.')
        if invalid_prices:
            st.warning('Pricing supports valid .com targets only. Skipped: ' + ', '.join(invalid_prices))
        st.caption('Active pricing provider: GoDaddy v3 (free read-only availability). '
                   'Uses your local GODADDY_PAT or ignored .env.godaddy file only after clicking the button. '
                   'Up to 25 names per batch; 59 uncached names need 3 requests. No purchases or research calls. '
                   'The Namecheap adapter remains available in code, but is not called by this button.')
        if st.button('Check Domain Prices', disabled=not pending_prices, key=project_key('check_prices'), width='stretch'):
            try:
                provider = GoDaddyPricing()
                price_progress = st.progress(0.0, text=f'Checking 0 of {len(pending_prices)} domains')
                def show_price_progress(completed, total):
                    price_progress.progress(completed / total, text=f'Checked and saved {completed} of {total} domains')
                with st.spinner('Checking registrar quotes only (no brand research)...'):
                    count = check_prices(p, pending_prices, provider, persist, progress=show_price_progress)
                st.session_state.saved_notice = f'Saved domain pricing checks for {count} domains.'
                unpriced = [d for d in pending_prices if not p.get('domain_pricing', {}).get(d, {}).get('verified')]
                if unpriced:
                    st.session_state.pricing_warning = (
                        'No reliable acquisition price returned for: ' + ', '.join(unpriced) +
                        '. These domains are not Affordable. See Pricing notes in the shortlist table.')
                research_by_name = latest_results(p)
                provisional = sum(pricing_fields(p, n, research_row=research_by_name.get(n.casefold()))['Budget classification'] == PROVISIONAL
                                  for n in p['names'])
                if provisional:
                    st.session_state.pricing_warning = (
                        st.session_state.get('pricing_warning', '') +
                        f' {provisional} candidates have provisional under-budget quotes, excluding taxes/ICANN fees. '
                        'They are not in Affordable Domains until the final total is verified.')
                st.rerun()
            except PricingError as exc:
                st.error(str(exc))

        st.subheader('Domain Pricing Results')
        cached_rows = cached_pricing_rows(p)
        pricing_counts = {label: len(filter_pricing_rows(cached_rows, label)) for label in RESULT_FILTERS}
        st.write(f"Checked domains: {pricing_counts['All checked domains']} · "
                 f"Provisional under $100: {pricing_counts['Provisional under $100']} · "
                 f"Verified affordable: {pricing_counts['Verified affordable']} · "
                 f"Over budget: {pricing_counts['Over budget']} · "
                 f"Stale quotes: {pricing_counts['Stale quotes']} · "
                 f"Unavailable or unknown price: {pricing_counts['Unavailable or unknown price']}")
        st.caption('Counts cover all checked domains before the view and search; stale over-budget quotes appear in both counts. '
                   'Saved cache is independent of shortlist decisions and lookup scope. '
                   'Provisional totals exclude unverified charges and are not Affordable. '
                   'Verified affordable requires a fresh, confirmed final all-in total within your budget. '
                   'Stale quotes and failed refreshes retain the last known price and require verification.')
        pricing_view = st.selectbox('Show pricing results', RESULT_FILTERS,
                                    index=RESULT_FILTERS.index('All checked domains'),
                                    key=project_key('pricing_results_filter'))
        pricing_search = st.text_input('Search pricing results', placeholder='Candidate, domain, or pricing notes',
                                       key=project_key('pricing_results_search'))
        visible_prices = filter_pricing_rows(cached_rows, pricing_view, pricing_search)
        st.caption(f'{len(visible_prices)} of {len(cached_rows)} checked domains shown. '
                   'Sorted by quoted acquisition price, with provisional candidates first. Tap a column to sort. '
                   'Swipe horizontally for price components, renewal, dates and notes; candidate names stay visible.')
        pricing_column_order = ('Candidate names', 'Acquisition price (USD)', 'Budget classification',
                                *(column for column in RESULT_COLUMNS
                                  if column not in ('Candidate names', 'Acquisition price (USD)', 'Budget classification')))
        st.dataframe(pd.DataFrame(visible_prices, columns=RESULT_COLUMNS), hide_index=True, height=440, row_height=44,
                     key=project_key('pricing_results_table'),
                     column_order=pricing_column_order,
                     column_config={**PRICE_COLUMNS,
                                    'Candidate names': st.column_config.TextColumn('Candidate name', width='medium', pinned=True),
                                    'Acquisition price (USD)': st.column_config.NumberColumn('Quoted total (USD)', format='dollar', width='small'),
                                    'Budget classification': st.column_config.TextColumn(width='medium')},
                     alt='Cached domain prices, availability, acquisition totals and provisional or verified classifications')
        st.download_button('Download pricing results CSV', pricing_results_csv(visible_prices),
                           'domain_pricing_results.csv', 'text/csv', disabled=not visible_prices,
                           key=project_key('pricing_results_csv'), on_click='ignore', width='stretch')
        if not cached_rows:
            st.info('No saved pricing results yet. Check Domain Prices when you are ready; no automatic requests are made.')
        elif not visible_prices:
            st.info('No saved quotes match this view. Choose All checked domains or clear the search to see more.')

        if p['names']:
            st.caption('GoDaddy marketplace quotes include separate premium purchase fees when returned. '
                       'Unavailable or incomplete listings remain unknown. Record a verified listing or final all-in '
                       'total below; leave unknown prices blank, never estimate. '
                       'Prices written in review notes alone do not become structured pricing data; use Save observed listing.')
            with st.form(project_key('observed_domain_quote')):
                quote_name = st.selectbox('Listing candidate', p['names'], key=project_key('quote_candidate'))
                quote_availability = st.selectbox('Listing registration availability',
                                                   ['Registered', 'Available', 'Unknown'])
                quote_premium = st.selectbox('Listing premium status', ['Unknown', 'Yes', 'No'])
                quote_price = st.text_input('Observed acquisition asking price (USD, optional)', help='Enter digits such as 1750 or 19.98, without a dollar sign or commas.')
                quote_renewal = st.text_input('Observed annual renewal price (USD, optional)', help='Leave blank when the annual renewal price is unknown.')
                quote_source = st.text_input('Registrar/listing source and URL or reference')
                quote_confirm = st.checkbox('I verified this current listing, including mandatory listed acquisition fees (not an estimate)')
                quote_total_confirm = st.checkbox('I verified the final all-in acquisition total, including tax, ICANN and all mandatory charges')
                if st.form_submit_button('Save observed listing', width='stretch'):
                    try:
                        if not quote_confirm:
                            raise ValueError('Confirm that you verified the current registrar listing before saving.')
                        domain = targets_for(p, quote_name)['Domain'].strip().lower()
                        record = observed_quote(domain, quote_availability,
                                                {'Unknown': None, 'Yes': True, 'No': False}[quote_premium],
                                                quote_price, quote_renewal, quote_source,
                                                total_verified=quote_total_confirm)
                        save_quote(p, domain, record)
                        p.setdefault('domain_pricing_attempts', {})[domain] = {
                            'checked_at': record['checked_at'], 'provider': 'User observation',
                            'status': 'Completed', 'availability': record['availability'],
                            'quote_returned': record['verified'], 'error': ''}
                        if persist():
                            st.session_state.saved_notice = 'Saved observed domain listing. Brand decisions are unchanged.'
                            st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))

    rows = export_rows(p)
    research_by_name = latest_results(p)
    price_rows = {r['Name']: pricing_fields(p, r['Name'], research_row=research_by_name.get(r['Name'].casefold())) for r in rows}
    if rows:
        st.caption('Google and registrar first. Your decisions are saved automatically.')
        stats = [sum(r.get('Decision', 'Undecided') == decision for r in rows)
                 for decision in ('Undecided', 'Keep', 'Maybe', 'Reject')]
        research_totals = {'Complete': 0, 'Incomplete': 0, 'New': 0}
        prior_summary = latest_results(p)
        for candidate_name in p['names']:
            research_totals[research_state(p, candidate_name, prior_summary.get(candidate_name.casefold()))] += 1
        st.caption(f"All candidates — research: {research_totals['Complete']} complete · {research_totals['Incomplete']} incomplete · {research_totals['New']} not yet researched")
        st.caption('Decision counts below cover all candidates, before the name filter or search.')
        cols = st.columns(4)
        for col, title, value in zip(cols, ('To review', 'Keep', 'Investigate', 'Discard'), stats):
            col.metric(title, value)

        shortlist_filter = 'Shortlist (Keep + Investigate)'
        filter_options = [shortlist_filter, 'Affordable Domains', 'All', 'Not reviewed', 'Strong conflicts', 'Possible overlaps',
                          'Little evidence', 'Keep', 'Investigate', 'Discard',
                          'Automatic shortlist', 'Automatic rejects']
        filter_key = project_key('quick_filter_v2')
        if st.session_state.get(filter_key) not in filter_options:
            st.session_state[filter_key] = 'All'
        filter_name = st.selectbox('Show names', filter_options, key=filter_key)
        search = st.text_input('Find name', key=project_key('quick_search')).strip().casefold()
        def eligible(r):
            if search and search not in r['Name'].casefold():
                return False
            decision = r.get('Decision', 'Undecided')
            conflict = str(r.get('Conflict assessment') or '').strip().casefold()
            return (filter_name == 'All' or
                    (filter_name == shortlist_filter and decision in ('Keep', 'Maybe')) or
                    (filter_name == 'Affordable Domains' and price_rows[r['Name']]['Domain eligibility'] == 'Eligible') or
                    (filter_name == 'Not reviewed' and decision == 'Undecided') or
                    (filter_name == 'Strong conflicts' and conflict == 'strong conflict') or
                    (filter_name == 'Possible overlaps' and conflict == 'possible overlap') or
                    (filter_name == 'Little evidence' and conflict == 'little evidence') or
                    (filter_name == 'Keep' and decision == 'Keep') or
                    (filter_name == 'Investigate' and decision == 'Maybe') or
                    (filter_name == 'Discard' and decision == 'Reject') or
                    (filter_name == 'Automatic shortlist' and r.get('Automatic recommendation') == 'Shortlist'
                     and r.get('Recommendation') == 'Shortlist') or
                    (filter_name == 'Automatic rejects' and r.get('Automatic recommendation') == 'Reject'))
        candidates = [r for r in rows if eligible(r)]
        st.markdown(f'### {len(candidates)} of {len(rows)} names match this view')
        st.write('Only your actions save Keep/Investigate/Discard decisions. Screening recommendations use evidence and any conflict-assessment override; historical saved choices are unchanged.' +
                 (' Name search is also applied.' if search else ''))
        if filter_name in ('Automatic shortlist', 'Automatic rejects'):
            st.caption('Automatic shortlist requires both the automatic and override-aware recommendation to shortlist. '
                       'Automatic rejects shows pre-override reject recommendations.')
        if candidates:
            index_key = project_key('quick_index_' + filter_name)
            shortlist_frame = None
            if filter_name in (shortlist_filter, 'Affordable Domains', 'Automatic shortlist'):
                shortlist_rows = []
                for candidate in candidates:
                    checked_at = pd.to_datetime(candidate.get('Checked at'), errors='coerce', utc=True)
                    shortlist_rows.append({
                        'Rank': candidate['Rank'],
                        'Candidate name': candidate['Name'],
                        'Current decision': 'Investigate' if candidate['Decision'] == 'Maybe' else candidate['Decision'],
                        'Automatic recommendation': candidate['Automatic recommendation'],
                        'Recommendation': candidate['Recommendation'],
                        'Recommendation reason': candidate['Recommendation reason'],
                        'Ranking reason': candidate['Ranking reason'],
                        'Automatic conflict assessment': candidate.get(
                            'Automatic conflict assessment', candidate.get('Conflict assessment', 'Unknown')),
                        '.com registration status': candidate.get('Domain status', 'Unknown'),
                        'Research completeness': candidate.get('Research', 'Unknown'),
                        'Last research date': checked_at.strftime('%Y-%m-%d') if not pd.isna(checked_at) else '',
                        **price_rows[candidate['Name']],
                    })
                shortlist_frame = pd.DataFrame(shortlist_rows)
                member_signature = hashlib.sha1(
                    '\0'.join(row['Candidate name'] for row in shortlist_rows).encode('utf-8')
                ).hexdigest()[:12]
                st.subheader('Shortlist' if filter_name == shortlist_filter else
                             'Ranked automatic shortlist' if filter_name == 'Automatic shortlist' else 'Affordable Domains')
                st.caption('Rank reflects the full candidate list. Select a row to open it below; sort by any column header. '
                           'Automatic recommendations do not change saved decisions.')
                selection = st.dataframe(
                    shortlist_frame,
                    key=project_key('shortlist_table_' + member_signature),
                    hide_index=True,
                    height=360,
                    row_height=42,
                    on_select='rerun',
                    selection_mode='single-row',
                    column_config={
                        **PRICE_COLUMNS,
                        'Candidate name': st.column_config.TextColumn(width='medium'),
                        'Current decision': st.column_config.TextColumn(width='small'),
                        'Automatic conflict assessment': st.column_config.TextColumn(width='medium'),
                        '.com registration status': st.column_config.TextColumn(width='medium'),
                        'Research completeness': st.column_config.TextColumn(width='medium'),
                        'Last research date': st.column_config.TextColumn(width='small'),
                    },
                    alt='Shortlisted candidates and their decisions, assessments, registration, and research status',
                )
                selected_rows = selection.selection.rows
                if selected_rows:
                    selected_name = shortlist_frame.iloc[selected_rows[0]]['Candidate name']
                    st.session_state[index_key] = next(
                        i for i, candidate in enumerate(candidates) if candidate['Name'] == selected_name)
            index = min(st.session_state.get(index_key, 0), len(candidates) - 1)
            st.session_state[index_key] = index
            item = candidates[index]
            name = item['Name']
            key = name.casefold()
            review = p['reviews'].get(key, {})
            with st.container(border=True):
                decision_label = {'Undecided': 'To review', 'Keep': 'Keep',
                                  'Maybe': 'Investigate', 'Reject': 'Discard'}.get(item.get('Decision'), 'To review')
                decision_source = review.get('decision_source')
                st.markdown(f'**Saved decision: {decision_label}** · '
                            f'{"Historical automatic choice" if decision_source == "automatic" else "Saved choice"}')
                st.markdown('## ' + name)
                st.caption(f"Rank {item['Rank']} · Screening recommendation: {item['Recommendation']} · {item['Recommendation reason']}")
                if item['Research refresh warnings']:
                    st.warning(item['Research refresh warnings'])
                links = candidate_links(p, name)
                price_info = price_rows[name]
                st.caption(f"Domain: {price_info['Domain name']} | {price_info['Budget classification']}")
                st.caption(f"Status: {price_info['Registration availability']} · "
                           f"Acquisition: {price_info['Acquisition price (USD)'] if price_info['Acquisition price (USD)'] is not None else 'Unknown'} USD · "
                           f"Renewal: {price_info['Annual renewal price (USD)'] if price_info['Annual renewal price (USD)'] is not None else 'Unknown'} USD · "
                           f"{price_info['Price reliability']} · {price_info['Price-check date'] or 'date unknown'} · "
                           f"{price_info['Pricing source'] or 'source unknown'}")
                st.caption(price_info['Verification needed'])
                if price_info['Latest refresh error']:
                    st.warning(f"Latest pricing refresh {price_info['Latest refresh date']}: {price_info['Latest refresh error']}")
                with st.expander('Domain pricing details'):
                    st.dataframe(pd.DataFrame([price_info]), hide_index=True, column_config=PRICE_COLUMNS,
                                 alt='Domain registration, premium purchase fees, quoted total and price reliability')
                google, registrar = st.columns(2)
                google.link_button('🔎 Google Search', links['Search name on Google'], width='stretch', type='primary')
                registrar.link_button('🌐 Registrar Search (.com)', links['Check domain at registrar'], width='stretch',
                                      type='primary', disabled=price_info['Domain eligibility'] == 'Not eligible')
                # Overview: concise findings from saved research, not claims of clearance.
                domain_engine = str(item.get('Domain status') or 'Unknown')
                conflict_engine = str(item.get('Automatic conflict assessment') or item.get('Conflict assessment') or 'Unknown')
                research_engine = str(item.get('Research') or 'Unknown')
                st.markdown('#### Quick overview')
                summary_cols = st.columns(4)
                summary_cols[0].metric('Saved decision', decision_label)
                summary_cols[1].metric('Automatic conflict', conflict_engine)
                summary_cols[2].metric('.com registration', domain_engine)
                summary_cols[3].metric('Research status', research_engine)
                st.caption('Automatic findings are screening leads. A registered domain is not proof of a competing business; an unknown check is not availability.')
                st.markdown('**Your quick checklist**')
                domain_options = ['Not checked', 'Registered', 'Available at registrar', 'Unclear']
                conflict_options = ['Not checked', 'No obvious conflict', 'Possible conflict', 'Major conflict']
                domain_value = review.get('quick_domain', 'Not checked')
                conflict_value = review.get('quick_conflict', 'Not checked')
                st.radio('Is the .com registered? (after registrar search)', domain_options,
                         horizontal=True, index=domain_options.index(domain_value) if domain_value in domain_options else 0,
                         key=project_key('domain_' + key))
                st.radio('Are there conflicting Google results?', conflict_options,
                         horizontal=True, index=conflict_options.index(conflict_value) if conflict_value in conflict_options else 0,
                         key=project_key('conflict_' + key))
                chosen_domain = st.session_state[project_key('domain_' + key)]
                chosen_conflict = st.session_state[project_key('conflict_' + key)]
                if chosen_conflict == 'Major conflict':
                    st.warning('Quick assessment: likely discard due to a major competing-brand conflict. Confirm before rejecting.')
                elif chosen_conflict == 'Possible conflict':
                    st.info('Quick assessment: investigate the matching business and its industry.')
                elif chosen_domain == 'Registered':
                    st.info('Quick assessment: .com registered. This alone is not grounds for rejection; investigate ownership or resale.')
                elif chosen_domain == 'Available at registrar' and chosen_conflict == 'No obvious conflict':
                    st.success('Quick assessment: promising for deeper trademark screening; not yet cleared.')
                else:
                    st.info('Quick assessment: use Google and the registrar, then choose Keep, Investigate or Discard.')
                with st.expander('Detailed results · evidence, links and scoring', expanded=False):
                    st.write('**Screening recommendation (including any manual conflict override):**', item.get('Recommendation', 'Unknown'))
                    st.write('**Automatic recommendation (before conflict override):**', item.get('Automatic recommendation', 'Unknown'))
                    st.write('**Why:**', item.get('Recommendation reason') or 'No reason recorded')
                    st.write('**Ranking:**', item.get('Ranking reason') or 'Not ranked')
                    st.write('**Research completeness:**', research_engine)
                    st.write('**Domain record:**', domain_engine)
                    st.write('**Automatic conflict assessment:**', conflict_engine)
                    st.write('**Current conflict assessment:**', item.get('Conflict assessment', conflict_engine))
                    st.write('**Universal score:**', item.get('Universal score', 'Unknown'))
                    st.write('**Next action:**', item.get('Next action') or 'Review evidence')
                    st.link_button('Search USPTO trademarks', links['Trademark search (USPTO)'])
                    evidence = item.get('Conflict evidence') or []
                    if isinstance(evidence, list) and evidence:
                        st.dataframe(pd.DataFrame(evidence), hide_index=True, width='stretch',
                                     column_config={'url': st.column_config.LinkColumn('Source')})
                    else:
                        st.caption('No stored conflict evidence shown; this does not establish clearance.')
                    for saved_run in reversed(p.get('runs', [])):
                        for result in saved_run.get('results', []):
                            if result.get('name', '').casefold() == key:
                                with st.expander('Raw saved research record', expanded=False):
                                    st.json(result, expanded=False)
                                break
                        else:
                            continue
                        break
                st.markdown('**Your decision**')
                def record_decision(decision):
                    p['reviews'].setdefault(key, {}).update({
                        'decision': decision, 'decision_source': 'manual', 'quick_domain': chosen_domain,
                        'quick_conflict': chosen_conflict, 'updated_at': now()})
                    if persist():
                        # When reviewing undecided names, the chosen item disappears after saving.
                        if filter_name != 'Not reviewed':
                            st.session_state[index_key] = min(index + 1, len(candidates) - 1)
                        st.rerun()
                actions = st.columns(3)
                if actions[0].button('✓ Keep', key=project_key('keep_' + key), width='stretch'):
                    record_decision('Keep')
                if actions[1].button('? Investigate', key=project_key('maybe_' + key), width='stretch'):
                    record_decision('Maybe')
                if actions[2].button('✕ Discard', key=project_key('discard_' + key), width='stretch'):
                    record_decision('Reject')
                navigation = st.columns([1, 2, 1])
                if navigation[0].button('← Previous', disabled=index == 0, width='stretch'):
                    st.session_state[index_key] = index - 1
                    st.rerun()
                navigation[1].caption(f'{index + 1} / {len(candidates)} matching names')
                if navigation[2].button('Next →', disabled=index == len(candidates) - 1, width='stretch'):
                    st.session_state[index_key] = index + 1
                    st.rerun()
        else:
            st.success('No names match this filter. Choose another view.')
        with st.expander('Download decisions / full table'):
            output = pd.DataFrame([{'Name': r['Name'], 'Decision': r['Decision'],
                'Engine domain': r.get('Domain status', ''), 'Engine conflict': r.get('Conflict assessment', ''),
                'Registrar finding': p['reviews'].get(r['Name'].casefold(), {}).get('quick_domain', 'Not checked'),
                'Google finding': p['reviews'].get(r['Name'].casefold(), {}).get('quick_conflict', 'Not checked')}
                for r in rows])
            st.dataframe(output, hide_index=True, width='stretch')
            st.download_button('Download screening decisions (CSV)', output.to_csv(index=False).encode('utf-8-sig'),
                               'brand_screening_decisions.csv', 'text/csv')
    else:
        st.info('Add candidate names to start screening.')
    st.divider()
    st.subheader('Project management & research')
    st.caption('Optional tools for editing candidates, running research, viewing rankings, and exporting results.')
    show_management = st.toggle('Show project management and research tools', value=False, key=project_key('show_management'))
    if show_management:
        with st.expander('Detailed results · all-name rankings', expanded=False):
            st.markdown('##### All-name rankings (optional)')
            rows = export_rows(p)
            if rows:
                filter_text = st.text_input('Search all result fields (name, status, title, description, handle, URL or notes)', key='ranking_filter_' + p['id'])
                from engine import latest_results
                evidence_by_name = {}
                for saved_run in p['runs']:
                    for evidence_row in saved_run.get('results', []):
                        evidence_by_name.setdefault(evidence_row['name'].casefold(), []).append(evidence_row)
                shown = [r for r in rows if not filter_text or filter_text.casefold() in
                         (json.dumps(r, ensure_ascii=False) + json.dumps(evidence_by_name.get(r['Name'].casefold(), {}), ensure_ascii=False)).casefold()]
                table = pd.DataFrame(shown, columns=list(rows[0]))[['Rank', 'Name', 'Universal score', 'Brand priority points', 'Recommendation', 'Recommendation reason', 'Domain status', 'Domain eligibility', 'Conflict assessment', 'Overlap leads', 'Ranking confidence', 'Project points', 'Next action', 'Brand fit score', 'Decision', 'Research']]
                show_all = st.checkbox('Show all candidates', key='show_all_' + p['id'])
                st.dataframe(table if show_all else table.head(5), hide_index=True, width='stretch')
                st.caption(f"Showing {len(table) if show_all else min(5, len(table))} of {len(table)} matching candidates for {p['title']}.")
                st.caption('Rank: shortlist / investigate / reject, then evidence score, business-name priority, optional project checks, brand-fit score and name. Scores are screening judgments, not availability probabilities.')
                with st.expander('How ranking works'):
                    st.write('Domain: verified eligible 30, provisional promising 20, unresolved but potentially available 10, registered or over budget 0. Conflict: little evidence 35, possible overlap 15, strong or insufficient evidence 0. Coverage: up to 15 for completed required checks. Business-name priority uses your four completed ratings, worth up to 20 points, when available; otherwise length, formatting and a non-generic token each contribute up to 5. Prospecting/research terms are shown but not scored. These signals are proxies, not proof of quality. Strong conflict leads receive a Reject recommendation; missing research or unknown, stale or failed pricing requires investigation. Provisional quotes may be shortlisted but require final price verification. Manual conflict overrides affect the recommendation; saved decisions never change automatically. Leads are not trademark clearance.')
            else:
                st.info('Add candidate names below to build this project’s ranking.')
        st.divider()
        with st.form('project_brief_' + p['id']):
            st.subheader('1. Your project')
            title = st.text_input('Project name', key=project_key('title_editor'))
            platform = st.selectbox('Primary use', ['YouTube / gaming', 'Podcast', 'Business / other'], key=project_key('platform_editor'))
            brief = st.text_area('What is this brand about?', key=project_key('brief_editor'))
            audience = st.text_input('Who is it for?', key=project_key('audience_editor'))
            if st.form_submit_button('Save project details'):
                if not title.strip():
                    st.error('Enter a project name.')
                else:
                    changed_use = platform != p.get('platform')
                    p.update(title=title.strip(), platform=platform, brief=brief, audience=audience)
                    if changed_use:
                        p['research_profile'] = default_profile(platform)
                        for key in ['profile_terms', 'profile_youtube', 'profile_gmail', 'profile_social', 'profile_podcasts']:
                            st.session_state.pop(project_key(key), None)
                    if persist():
                        st.session_state.saved_notice = 'Project details saved.'
                        st.rerun()

        with st.expander('Project research profile', expanded=True):
            profile = profile_for(p)
            for base, value in {'profile_terms': profile['terms'], 'profile_youtube': profile['youtube'], 'profile_gmail': profile['gmail'], 'profile_social': profile['social'], 'profile_podcasts': profile['podcasts']}.items():
                st.session_state.setdefault(project_key(base), value)
            with st.form('profile_' + p['id']):
                terms = st.text_input('Industry / topic terms (comma-separated)', key=project_key('profile_terms'), help='Used to search for competing brands in your field and assess whether overlaps are relevant. For example: banking, lending, software, consulting.')
                cols = st.columns(4)
                yt_on = cols[0].checkbox('YouTube checks', key=project_key('profile_youtube'))
                gmail_on = cols[1].checkbox('Gmail review', key=project_key('profile_gmail'))
                social_on = cols[2].checkbox('Social searches', key=project_key('profile_social'))
                apple_on = cols[3].checkbox('Apple Podcasts', key=project_key('profile_podcasts'))
                if st.form_submit_button('Save research profile'):
                    if not terms.strip():
                        st.error('Enter at least one industry or topic term.')
                    else:
                        p['research_profile'] = {'terms': terms.strip(), 'youtube': yt_on, 'gmail': gmail_on, 'social': social_on, 'podcasts': apple_on}
                        if persist():
                            st.session_state.saved_notice = 'Profile saved. Run research to collect checks for the new profile.'
                            st.rerun()
            st.caption('Universal ranking: domain 30 points, conflict assessment 35, required-check coverage 15. Selected platform checks contribute up to 20 supporting points. A failed check earns no availability points.')

        st.subheader('2. Candidate names')
        upload = st.file_uploader('Import candidates.csv (including older project CSV files)', type=['csv'])
        if st.button('Import and merge names', disabled=upload is None):
            try:
                names = merge_candidate_csv(p, upload.getvalue())
                st.session_state.names_editor = '\n'.join(names)
                if persist():
                    st.session_state.saved_notice = f'Imported and merged. {len(names)} unique candidates are saved.'
                    st.rerun()
            except (UnicodeError, ValueError):
                st.error('Could not import this file. Use a UTF-8 CSV with a Name or Candidate heading, or one name per row.')
        st.text_area('One name per line', key='names_editor', height=230, on_change=autosave_names)
        if st.button('Save candidate names'):
            if autosave_names():
                st.session_state.saved_notice = f"Saved {len(p['names'])} unique candidates."
                st.rerun()
        st.caption('Edits save automatically when you click outside the box. Use the box below to append names without replacing your list.')
        with st.form('append_names', clear_on_submit=True):
            additions = st.text_area('Paste additional names (one per line)', help='Plain names, numbered lists, or copied name tables are accepted.')
            if st.form_submit_button('Add and save names'):
                try:
                    merged = clean_names(p['names'] + import_pasted(additions))
                    count = len(merged) - len(p['names'])
                    p['names'] = merged
                    if persist():
                        st.session_state.pending_names_editor = '\n'.join(merged)
                        st.session_state.names_added = count
                        st.rerun()
                except ValueError as e:
                    st.error(str(e))

        st.subheader('3. Research')
        st.caption('Only new and incomplete names are selected by default. Completed checks are reused per source; only failed, missing or changed-context checks are retried unless you force a refresh.')
        with st.expander('Optional automated searches'):
            youtube_key = st.text_input('YouTube Data API key', value=stored_key('YouTube'), type='password', key='youtube_key')
            google_key = st.text_input('SerpAPI key for Google search', value=stored_key('SerpAPI'), type='password', key='google_key')
            st.button('Save API keys on this computer', on_click=save_keys)
            if st.session_state.get('key_notice'):
                st.info(st.session_state.key_notice)
            st.caption('Keys remain in your computer credential store or environment variables; they are not exported with projects.')
        profile = profile_for(p)
        prior = latest_results(p)
        state_counts = {'Complete': 0, 'Incomplete': 0, 'New': 0}
        for candidate in p['names']:
            state_counts[research_state(p, candidate, prior.get(candidate.casefold()))] += 1
        st.caption(f"Saved candidates: {len(p['names'])} · Complete: {state_counts['Complete']} · Incomplete: {state_counts['Incomplete']} · New: {state_counts['New']}")
        mode = st.radio('Research mode', ['New and incomplete only', 'New only', 'All (force refresh)'], horizontal=True)
        queue = plan_research(p, mode)
        batch_size = st.number_input('Maximum names in this batch', min_value=1, max_value=250, value=10, step=1)
        selected = queue[:int(batch_size)]
        requests_per_name = 6 if profile['social'] else 3
        st.info(f'{len(queue)} names eligible · {len(selected)} selected now · up to {len(selected)*requests_per_name} SerpAPI searches estimated. This is an upper bound, not a quota guarantee.')
        if selected:
            with st.expander('Names selected for this batch'):
                st.write(', '.join(selected))
        if st.button('Research selected names', type='primary', disabled=not selected or bool(st.session_state.get('save_error'))):
            progress = st.progress(0, text='Starting research…')
            run = {'id': now(), 'brief': p['brief'], 'audience': p['audience'], 'platform': p['platform'],
                   'profile': profile_for(p), 'status': 'In progress', 'results': []}
            p['runs'].append(run)
            if persist():
                confirmed_count = 0
                checkpoint_failed = False
                try:
                    for i, name in enumerate(selected):
                        progress.progress(i / len(selected), text=f'Researching {name} ({i+1}/{len(selected)})')
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            row = pool.submit(research_name, name, youtube_key, google_key, False,
                                              targets_for(p, name), False, profile_for(p),
                                              previous=prior.get(name.casefold()), force=mode == 'All (force refresh)').result()
                        run['results'].append(row)
                        if not persist():
                            checkpoint_failed = True
                            break
                        confirmed_count += 1
                    if not checkpoint_failed:
                        run['status'] = 'Completed'
                        if persist():
                            progress.progress(1.0, text='Batch saved.')
                            st.session_state.saved_notice = f'Saved {confirmed_count} results. Run another batch to continue.'
                            st.rerun()
                        checkpoint_failed = True
                except Exception as exc:
                    run['status'] = 'Interrupted'
                    if not st.session_state.get('save_error'):
                        persist()
                    st.error(f'Research interrupted after {confirmed_count} confirmed saved names. Any unsaved results remain in memory; resolve save errors before continuing. ({type(exc).__name__})')
                if checkpoint_failed:
                    run['status'] = 'Interrupted'
                    st.error(f'Research stopped after {confirmed_count} confirmed saved names because a checkpoint failed. No further provider calls were made. Download the unsaved backup before reloading.')
            else:
                run['status'] = 'Interrupted'
                st.error('Research did not start because the initial checkpoint could not be saved. No provider calls were made.')
        else:
            st.info('No names are currently queued for this mode. Check the research counts above or select a different mode.')

        st.subheader('Advanced review (optional)')
        rows = export_rows(p)
        if rows:
            name = st.selectbox('Review a candidate', [r['Name'] for r in rows], key=f'candidate_{p["id"]}')
            review = p['reviews'].get(name.casefold(), {})
            assessed = next(r for r in rows if r['Name'] == name)
            st.write('**' + assessed['Recommendation'] + ':** ' + assessed['Recommendation reason'])
            st.caption(assessed['Ranking reason'])
            if assessed['Research refresh warnings']:
                st.warning(assessed['Research refresh warnings'])
            with st.expander('Research evidence dates (UTC)'):
                st.text(assessed['Research evidence dates'] or 'No completed evidence stored.')
            if assessed['Incomplete checks']:
                with st.expander('Why this research is incomplete', expanded=True):
                    for line in assessed['Incomplete checks'].splitlines():
                        st.write('• ' + line)
            with st.expander('Competing names and conflict evidence', expanded=bool(assessed['Conflict evidence'])):
                st.caption('Automatic rules consider name matching, your industry terms and whether the result resembles a brand page. Confirm relevant leads using their sources.')
                if assessed['Conflict evidence']:
                    st.dataframe(pd.DataFrame(assessed['Conflict evidence']), hide_index=True, width='stretch', column_config={'url': st.column_config.LinkColumn('Evidence source')})
                else:
                    st.write('No matching leads stored. Check research completeness before interpreting this as little evidence.')
            with st.form('review_' + p['id'] + '_' + name):
                st.write('Rate this name against your saved project brief and audience.')
                cols = st.columns(4)
                ratings = {}
                for col, field in zip(cols, RATING_FIELDS):
                    ratings[field] = col.selectbox(field, [None, 1, 2, 3, 4, 5], index=[None, 1, 2, 3, 4, 5].index(review.get(field)), format_func=lambda v: 'Not rated' if v is None else str(v))
                decision = st.selectbox('Your decision', DECISIONS, index=DECISIONS.index(review.get('decision', 'Undecided')))
                statuses = ['Unknown', 'Needs review', 'Confirmed available', 'Conflict found']
                domain = st.selectbox('Domain review', statuses, index=statuses.index(review.get('domain', 'Unknown')))
                social = st.selectbox('Social review', statuses, index=statuses.index(review.get('social', 'Unknown')))
                trademarks = ['Unknown', 'Needs review', 'Reviewed — no issue identified', 'Possible conflict']
                trademark = st.selectbox('Trademark review', trademarks, index=trademarks.index(review.get('trademark', 'Unknown')))
                assessment = st.selectbox('Conflict assessment override', CONFLICT_OPTIONS, index=CONFLICT_OPTIONS.index(review.get('conflict_assessment', 'Use automatic assessment')))
                conflict_evidence = st.text_area('Conflict assessment evidence', review.get('conflict_evidence', ''), help='Use a source URL and explanation when overriding the automatic assessment. Your decision remains separate.')
                st.write('Proposed usernames and manual availability checks')
                st.caption('Check the exact username on the platform, then record the result and evidence. Changing a target clears its previous confirmation; save the new target before checking it.')
                existing_targets = targets_for(p, name)
                targets, availability = dict(existing_targets), dict(review.get('availability', {}))
                active = ['Domain']
                if profile_for(p)['youtube']:
                    active.append('YouTube')
                if profile_for(p)['gmail']:
                    active.append('Gmail')
                if profile_for(p)['social']:
                    active += ['Facebook', 'TikTok', 'Instagram']
                for platform_name in active:
                    cols = st.columns([2, 2, 3])
                    targets[platform_name] = cols[0].text_input(platform_name + ' username / domain', existing_targets[platform_name]).strip().lower().removeprefix('@')
                    manual = review.get('availability', {}).get(platform_name, {})
                    current = manual.get('status', 'Unknown')
                    if current not in AVAILABILITY:
                        current = 'Unknown'
                    status = cols[1].selectbox(platform_name + ' availability', AVAILABILITY, index=AVAILABILITY.index(current))
                    evidence = cols[2].text_input(platform_name + ' evidence / URL', manual.get('evidence', ''))
                    availability[platform_name] = {'status': status, 'evidence': evidence,
                                                    'target': targets[platform_name], 'checked_at': manual.get('checked_at', '')}
                    if status != manual.get('status', 'Unknown') or evidence != manual.get('evidence', ''):
                        availability[platform_name]['checked_at'] = now()
                notes = st.text_area('Notes and evidence', review.get('notes', ''))
                if st.form_submit_button('Save review'):
                    changed = save_review(p, name, {**ratings, 'decision': decision, 'decision_source': 'manual', 'domain': domain, 'social': social, 'trademark': trademark, 'notes': notes, 'updated_at': now(), 'targets': targets, 'availability': availability, 'conflict_assessment': assessment, 'conflict_evidence': conflict_evidence})
                    if changed:
                        st.session_state.target_notice = 'Targets changed for ' + ', '.join(changed) + '. Their prior confirmations were cleared. Check these targets and save the new results.'
                    if persist():
                        st.rerun()
            st.write('Research links')
            st.caption('One link per selected platform, using this candidate’s saved username or domain. Broad search results remain in research history below.')
            cols = st.columns(3)
            for i, (label, url) in enumerate(candidate_links(p, name).items()):
                cols[i % 3].link_button(label, url)
            if p['runs']:
                run = st.selectbox('Research history', list(reversed(range(len(p['runs'])))), format_func=lambda i: p['runs'][i]['id'])
                st.caption('Research belongs to this project. UTC timestamps identify each saved run.')
                found = next((r for r in p['runs'][run]['results'] if r['name'].casefold() == name.casefold()), None)
                if found:
                    if found.get('registration'):
                        st.write('**Domain registration:** ' + found['registration']['status'] + ' — ' + found['registration']['message'])
                        st.link_button('Registry evidence', found['registration']['url'])
                    if found.get('youtube_handle'):
                        st.write('**YouTube handle:** ' + found['youtube_handle']['status'] + ' — ' + found['youtube_handle']['message'])
                    if profile_for(p)['gmail']:
                        st.write('**Gmail:** Check the proposed username in Google signup, then record it in the review above.')
                    st.write(f"**.com DNS:** {found['dns']['status']} — {found['dns']['message']}")
                    for check in found['checks']:
                        with st.expander(check['source'] + ' · ' + check['status']):
                            st.write(check['message'])
                            if check.get('details_error'):
                                st.warning('Channel details incomplete: ' + check['details_error'])
                            if check['exact_count'] is not None:
                                exact = sum(classify_match(name, m.get('title', ''))[0] == 'Exact title' for m in check['matches'])
                                st.write(f"Exact normalized title matches in this sample: {exact}")
                            st.link_button('Open search', check['url'])
                            if check['matches']:
                                displayed = []
                                for match in check['matches']:
                                    kind, fields = classify_match(name, match.get('title', ''), match.get('description', ''), match.get('url', ''), match.get('handle', ''))
                                    displayed.append(match | {'match': kind, 'matched_fields': fields})
                                st.dataframe(pd.DataFrame(displayed), hide_index=True, width='stretch', column_config={'url': st.column_config.LinkColumn('Source')})
                else:
                    st.info('This candidate was not included in that run.')
            st.download_button('Download shortlist CSV', csv_bytes(p), 'brand_shortlist.csv', 'text/csv')
            if st.button('Prepare full Excel results'):
                try:
                    st.session_state.excel_download = excel_bytes(workbook_data(p))
                    st.session_state.excel_project = p['id']
                    st.session_state.excel_revision = p.get('updated_at')
                except Exception:
                    st.error('Could not prepare the workbook. Check that excel_template.xlsx is in the app folder.')
            if st.session_state.get('excel_download') and st.session_state.get('excel_project') == p['id']:
                if st.session_state.get('excel_revision') == p.get('updated_at'):
                    st.download_button('Download full Excel results', st.session_state.excel_download, 'brand_full_results.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                else:
                    st.info('Results changed. Click Prepare full Excel results to refresh your workbook.')
        else:
            st.info('Add and save candidate names to begin.')
        st.download_button('Download full project backup', json.dumps(p, ensure_ascii=False, indent=2), 'brand_project_backup.json', 'application/json')
