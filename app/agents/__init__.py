"""Agents: automations with several steps, run by one step runner (plans/12 phase 1).

An agent is a spec (steps.AgentSpec): a typed context and an ordered list of steps. Code does
every step it can; model steps get named fields in and return schema-validated fields out
(model_step). A gate can stop a run as skipped, and a pause step waits on Pryce through the
approvals table. Each step is recorded on the automation run (automation_runs.steps).

- steps: the step kinds, AgentSpec, Outcome
- runner: runs a spec, records steps, pauses and resumes
- model_step: the model step contract (template, JSON reply, schema, one repair call)
- context: shared providers (stack facts, code search, releases)
- approvals: the approvals table
- scout, legacy: the agents (scout; triage and the map check wrapped as one step each)
"""
