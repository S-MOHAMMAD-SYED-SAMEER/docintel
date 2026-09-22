"""Demo Mode: a deterministic, credential-free demonstration of the real
DocIntel application.

Everything here is outside `app/` on purpose. `demo/app.py` builds the real
`app.main.create_app()` and overrides exactly one dependency -- the
extraction provider -- with `demo.providers.DemoExtractionProvider`, a
deterministic stand-in that replays committed, hand-verified fixture
answers for a small set of known sample invoices. No route, no validation
rule, no scoring logic, no persistence, and no review/correction/export
code is duplicated or reimplemented here. See docs/DEMO.md.
"""
