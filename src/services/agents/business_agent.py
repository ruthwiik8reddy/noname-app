"""Business priorities share exactly the same definitions as the owner dashboard."""
from .base import BaseAgent, Finding
from ..intelligence.facts import IntelligenceFacts


class BusinessAgent(BaseAgent):
    name = 'business'
    label = 'Business intelligence'
    description = 'Revenue changes, rebooking candidates and evidence-backed operating priorities.'
    icon = 'chart'
    default_interval_mins = 60
    responds_to = ('job_completed','job_assigned','estimate_created','booking_created','stock_changed','dvi_analyzed')

    def collect(self):
        snapshot = IntelligenceFacts().snapshot(self.studio_id)
        facts = {f['id']:f for f in snapshot['facts']}
        return [Finding(kind=a['id'], title=a['title'], severity=a['severity'],
                        detail=' '.join(facts[key]['statement'] for key in a['fact_ids']),
                        action_label='Review intelligence', action_url='/intelligence/',
                        fingerprint='business:'+a['id'],
                        data={'generated_at':snapshot['generated_at'],'period':snapshot['period'],
                              'facts':[facts[key] for key in a['fact_ids']]}) for a in snapshot['actions']]
