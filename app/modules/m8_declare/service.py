from app.modules.m8_declare.policy import plan,digest,stable,rule_signature

def plan_declaration(state,context_lookup=None):
    """Compute the declarable result for this payroll batch.

    context_lookup(period, signature) returns the prior-filing context from the
    store, or None. The lookup is injected so run() has no storage side effects
    and stays unit-testable.

    filing_id is derived from (period, batch identity, rule signature) only,
    never from the evolving plan: after the first filing the same batch recomputes
    with every confirmed item marked 已申报, so a plan-derived id would look like
    a new filing on every re-run. With this id:
    * re-running an identical batch yields the same id and submit returns the
      stored declaration (no duplicate, no supersede);
    * a new payroll batch or a changed rule/parameter version has a different
      run_id/signature, so it becomes a new filing; the submission voids the
      prior active declaration and marks this one 待复核.
    """
    baseline=plan(state)
    prior=None
    if context_lookup is not None:
        prior=context_lookup(baseline['period'],baseline['rule_signature'])
    body=plan(state,prior)
    body['prior_filed']=len(prior.get('filed_ids') or []) if prior else 0
    body['filing_id']=stable({'period':body['period'],'run_id':state.get('run_id'),
        'rule_signature':body['rule_signature']})
    body['plan_digest']=digest(body)
    return body
