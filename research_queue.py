"""Safe batch planning against persisted project results."""
from assessment import assess
from engine import latest_results


def research_state(project, name, row):
    if row is None:
        return 'New'
    assessment = assess(project, name, row)
    return 'Incomplete' if assessment.get('Incomplete checks') or row.get('refresh_failures') else 'Complete'


def plan_research(project, mode='New and incomplete only'):
    latest = latest_results(project)
    result = []
    for name in project['names']:
        state = research_state(project, name, latest.get(name.casefold()))
        if mode == 'All (force refresh)' or (mode == 'New only' and state == 'New') or (mode == 'New and incomplete only' and state != 'Complete'):
            result.append(name)
    return result
