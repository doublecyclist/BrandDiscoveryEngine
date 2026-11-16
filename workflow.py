"""The four-stage workflow, using the existing project and screening data."""
import csv
import io
import re

import pandas as pd
import streamlit as st

from domain_pricing import AFFORDABLE, OVER_BUDGET, budget_for, pricing_fields
from engine import clean_names, export_rows, import_pasted, latest_results, merge_candidate_csv, now


STAGES = ('Generate Names', 'Screen Names', 'Build the Shortlist', 'Choose the Final Five')
DECISION_LABELS = {'Undecided': 'To review', 'Keep': 'Keep', 'Maybe': 'Investigate', 'Reject': 'Discard'}


def generate_names(text):
    """Suggest simple combinations of supplied words without external services."""
    words = clean_names(re.findall(r"[^\W\d_]+", text, flags=re.UNICODE))
    if len(words) < 2:
        raise ValueError('Enter at least two words to combine.')
    if len(words) > 10:
        raise ValueError('Enter no more than 10 keywords for one batch.')
    return clean_names([first[:1].upper() + first[1:] + second[:1].upper() + second[1:]
                        for first in words for second in words if first.casefold() != second.casefold()])


def _add_names(project, names, persist):
    merged = clean_names(project['names'] + names)
    count = len(merged) - len(project['names'])
    project['names'] = merged
    if persist():
        st.session_state.pending_names_editor = '\n'.join(merged)
        st.session_state.names_added = count
        st.rerun()


def _price_details(project, rows):
    research = latest_results(project)
    return {row['Name']: pricing_fields(project, row['Name'], research_row=research.get(row['Name'].casefold()))
            for row in rows}


def _screen_row(row, price):
    return {
        'Name': row['Name'],
        'Automatic conflict': row['Automatic conflict assessment'],
        '.com status': price['Registration availability'],
        'Acquisition price (USD)': price['Acquisition price (USD)'],
        'Price status': price['Budget classification'] + ' | ' + price['Price reliability'],
        'Automatic recommendation': row['Automatic recommendation'],
        'Last research date': row.get('Checked at') or 'Not checked',
    }


def _final_row(row, price):
    verified = (price['Acquisition price (USD)']
                if price['Price reliability'] == 'Final total verified'
                and price['Budget classification'] in (AFFORDABLE, OVER_BUDGET) else None)
    remaining = price['Verification needed'] + ' Review conflict evidence and trademark rights.'
    if row.get('Incomplete checks'):
        remaining += ' Incomplete research: ' + row['Incomplete checks'].replace('\n', '; ')
    if row.get('Research refresh warnings'):
        remaining += ' Later research refresh failed: ' + row['Research refresh warnings'].replace('\n', '; ')
    return {
        'Name': row['Name'],
        'Rank': row['Rank'],
        'Why promising / recommendation': row['Recommendation reason'],
        'Potential conflicts': ('Automatic: ' + row['Automatic conflict assessment']
                                + '; current assessment: ' + row['Conflict assessment']),
        '.com domain': price['Domain name'] + ' | ' + price['Registration availability'],
        'Verified acquisition price (USD)': verified,
        'Price status': price['Budget classification'] + ' | ' + price['Price reliability'],
        'Outstanding verification': remaining,
        'Last research date': row.get('Checked at') or 'Not checked',
        'Last price date': price['Price-check date'] or 'Not checked',
    }


def _report_csv(rows):
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    for row in rows:
        writer.writerow({key: "'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@'))
                         else value for key, value in row.items()})
    return output.getvalue().encode('utf-8-sig')


def render_workflow(project, persist, project_key):
    stage = st.radio('Workflow stage', STAGES, horizontal=True, key=project_key('workflow_stage'))
    st.caption('Your project stays selected across stages. Advanced research, pricing and review tools are below.')

    if stage == STAGES[0]:
        st.subheader('1. Generate Names')
        st.caption('Combine your own keywords locally, or paste and import names from other sources. Nothing is sent to a service.')
        terms = st.text_area('Naming themes or keywords', placeholder='For example: beacon insight harbor',
                             key=project_key('workflow_terms'))
        if st.button('Generate name ideas', key=project_key('generate_names')):
            try:
                st.session_state[project_key('generated_names')] = generate_names(terms)
            except ValueError as exc:
                st.session_state.pop(project_key('generated_names'), None)
                st.error(str(exc))
        ideas = st.session_state.get(project_key('generated_names'), [])
        if ideas:
            st.write('Generated ideas:', ', '.join(ideas))
            st.caption('These are simple word combinations, not a check of quality, conflicts or availability.')
            if st.button('Add generated ideas', key=project_key('add_generated')):
                _add_names(project, ideas, persist)
        with st.form(project_key('workflow_paste'), clear_on_submit=True):
            pasted = st.text_area('Paste candidate names', help='One per line; numbered lists and copied tables are supported.')
            if st.form_submit_button('Add pasted names'):
                try:
                    names = import_pasted(pasted)
                    if not names:
                        raise ValueError('Enter at least one candidate name.')
                    _add_names(project, names, persist)
                except ValueError as exc:
                    st.error(str(exc))
        upload = st.file_uploader('Import candidate names (CSV)', type=['csv'], key=project_key('workflow_upload'))
        if st.button('Import names', disabled=upload is None, key=project_key('workflow_import')):
            try:
                before = len(project['names'])
                merged = merge_candidate_csv(project, upload.getvalue())
                if persist():
                    st.session_state.pending_names_editor = '\n'.join(merged)
                    st.session_state.names_added = len(merged) - before
                    st.rerun()
            except (UnicodeError, ValueError):
                st.error('Could not import this file. Use a UTF-8 CSV with a Name or Candidate heading, or one name per row.')
        st.caption(f"{len(project['names'])} unique candidates saved. Adding another batch keeps earlier names and decisions; duplicates are skipped.")
        return

    rows = export_rows(project)
    prices = _price_details(project, rows)
    if not rows and stage != STAGES[3]:
        st.info('Add candidate names in Generate Names to begin.')
        return

    if stage == STAGES[1]:
        st.subheader('2. Screen Names')
        st.caption(f"Acquisition budget: ${budget_for(project):.2f} maximum. Unknown prices are not automatic rejections. "
                   'Verified, provisional, stale and unavailable prices are labeled separately.')
        search = st.text_input('Filter screening names', key=project_key('workflow_screen_search')).strip().casefold()
        shown = [row for row in rows if search in row['Name'].casefold()]
        st.caption(f'{len(shown)} of {len(rows)} candidates shown.')
        if shown:
            st.dataframe(pd.DataFrame([_screen_row(row, prices[row['Name']]) for row in shown]),
                         hide_index=True, width='stretch', alt='Candidate screening and domain price verification')
            name = st.selectbox('Inspect conflict evidence', [row['Name'] for row in shown],
                                key=project_key('workflow_screen_candidate'))
            selected = next(row for row in shown if row['Name'] == name)
            price = prices[name]
            st.caption(f"{price['Domain name']} · {price['Verification needed']} · "
                       f"Price checked: {price['Price-check date'] or 'not checked'}")
            if price['Latest refresh error']:
                st.warning('Latest pricing refresh failed: ' + price['Latest refresh error'])
            if selected['Research refresh warnings']:
                st.warning(selected['Research refresh warnings'])
            if selected['Automatic conflict assessment'] == 'Strong conflict':
                st.warning('Strong competing-brand lead: inspect the evidence before making a decision.')
            with st.expander('View conflict evidence'):
                evidence = selected.get('Conflict evidence') or []
                if evidence:
                    st.dataframe(pd.DataFrame(evidence), hide_index=True, width='stretch',
                                 column_config={'url': st.column_config.LinkColumn('Source')})
                else:
                    st.info('No conflict leads are stored. This is not trademark clearance.')
        st.caption('Run research or check domain prices in Research, pricing and advanced tools below. Searches can incur provider charges and only run when requested.')
        return

    if stage == STAGES[2]:
        st.subheader('3. Build the Shortlist')
        view = st.selectbox('Show shortlist candidates', ('Keep and Investigate', 'All candidates', 'Keep', 'Investigate', 'Discard', 'To review'),
                            key=project_key('workflow_shortlist_filter'))
        decisions = {'Keep and Investigate': ('Keep', 'Maybe'), 'Keep': ('Keep',),
                     'Investigate': ('Maybe',), 'Discard': ('Reject',), 'To review': ('Undecided',)}
        shown = [row for row in rows if view == 'All candidates' or row['Decision'] in decisions[view]]
        st.caption(f'{len(shown)} of {len(rows)} candidates shown. Saved decisions are separate from automatic recommendations.')
        if shown:
            table = []
            for row in shown:
                price = prices[row['Name']]
                table.append({'Name': row['Name'], 'Rank': row['Rank'], 'Recommendation': row['Recommendation'],
                              'Automatic conflict': row['Automatic conflict assessment'],
                              'Current conflict': row['Conflict assessment'], 'Domain eligibility': price['Domain eligibility'],
                              'Acquisition price (USD)': price['Acquisition price (USD)'],
                              'Price status': price['Budget classification'] + ' | ' + price['Price reliability'],
                              'Saved decision': DECISION_LABELS.get(row['Decision'], row['Decision']),
                              'Ranking explanation': row['Ranking reason']})
            st.dataframe(pd.DataFrame(table), hide_index=True, width='stretch',
                         alt='Ranked shortlist with saved decisions and screening reasons')
        name = st.selectbox('Change decision for', [row['Name'] for row in rows],
                            key=project_key('workflow_decision_candidate'))
        selected = next(row for row in rows if row['Name'] == name)
        st.caption(f"Current decision: {DECISION_LABELS.get(selected['Decision'], selected['Decision'])} · "
                   f"Automatic recommendation: {selected['Automatic recommendation']}")
        buttons = st.columns(3)
        for column, label, value in zip(buttons, ('Keep', 'Investigate', 'Discard'), ('Keep', 'Maybe', 'Reject')):
            if column.button(label, key=project_key('workflow_decision_' + value), width='stretch'):
                project['reviews'].setdefault(name.casefold(), {}).update(
                    {'decision': value, 'decision_source': 'manual', 'updated_at': now()})
                if persist():
                    st.rerun()
        return

    st.subheader('4. Choose the Final Five')
    st.caption('Choose up to five names. Changing this selection does not change decisions, research or prices.')
    saved = project.get('finalists', [])
    if (not isinstance(saved, list) or len(saved) > 5
            or any(not isinstance(name, str) or not name.strip() for name in saved)
            or len({name.casefold() for name in saved}) != len(saved)):
        st.error('The saved finalist list is invalid. It has not been changed; export a project backup and inspect it before editing.')
        return
    options = [row['Name'] for row in rows]
    missing = [name for name in saved if name not in options]
    options.extend(missing)
    if missing:
        st.warning('Some finalists are no longer in the candidate list. They remain saved; add them back to see their current screening details.')
    with st.form(project_key('workflow_finalists')):
        selected = st.multiselect('Finalists (maximum five)', options, default=saved, max_selections=5,
                                  key=project_key('workflow_finalist_selection'))
        if st.form_submit_button('Save finalists'):
            if len(selected) > 5:
                st.error('Choose no more than five finalists.')
            else:
                project['finalists'] = selected
                if persist():
                    st.session_state.saved_notice = 'Finalists saved. Research and decisions are unchanged.'
                    st.rerun()
    by_name = {row['Name']: row for row in rows}
    report = []
    for name in saved:
        if name in by_name:
            row, price = by_name[name], prices[name]
            report.append(_final_row(row, price))
        else:
            report.append({'Name': name, 'Rank': None, 'Why promising / recommendation': 'Candidate not in current list',
                           'Potential conflicts': 'Unknown', '.com domain': 'Unknown',
                           'Verified acquisition price (USD)': None, 'Price status': 'Unknown',
                           'Outstanding verification': 'Restore candidate and review saved evidence.',
                           'Last research date': 'Unknown', 'Last price date': 'Unknown'})
    if report:
        st.dataframe(pd.DataFrame(report), hide_index=True, width='stretch',
                     alt='Selected finalists, conflicts, verified prices and remaining checks')
        st.download_button('Download final-five report (CSV)', _report_csv(report), 'brand_final_five.csv',
                           'text/csv', on_click='ignore')
    else:
        st.info('No finalists saved yet.')
    st.caption('Conflict screening is preliminary and does not provide legal trademark clearance. A provisional or stale price is not a verified checkout total.')
