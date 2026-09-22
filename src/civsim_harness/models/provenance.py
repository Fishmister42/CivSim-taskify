"""Who supplied a decision: the agent's own choice, or a declared script (T326).

**The question this module exists to answer, and why it has to be answerable from the
record alone.** ``provider/scripted.py`` executes a fixed, declared action sequence through
the harness's ordinary dispatch path so that an action chain can be *proven to work end to
end*. That is a harness capability test, and a capability test says **nothing whatsoever**
about whether an agent would have chosen the action. A landing produced by a script and a
landing produced by an agent's own choice are different claims about different things, and
the breadth/coverage scorecard only ever meant the second one. So the two must never share a
total, and which one a given record is must be readable **off the record**, not inferred from
a run configuration that may be missing, edited, or simply not written down.

**Where the answer lives, and why that field rather than a new one.**
:attr:`~civsim_harness.models.records.ModelCall.model_served` is a required field of a
required record: every ``DecisionStep`` carries a ``model_call_id`` (FR-042, SC-012), every
``ModelCall`` carries a ``model_served``, and
``store.port.DecisionStepBundle._cross_record_consistency`` *refuses to hold* a decision whose
``model_call_id`` disagrees with the model call beside it. So a decision cannot be separated
from the identity of whatever served it -- not by a missing column, not by a defaulted field,
not by an editor. That invariant already existed; this module only reads it.

A new ``Decision.decision_provenance`` field was considered and **deliberately not added**.
It would have needed a default in order to keep the millions of already-recorded
``bundle_json`` blobs readable, and a defaulted provenance flag is precisely this project's
own worst-known defect shape: *an optional parameter with a safe-looking empty default that
every unit test supplies and the one production call site does not*. A scripted decision that
forgot to set it would then have recorded itself as agent-chosen -- a record **better** than
the truth, which is the invisible, credibility-destroying direction. Deriving from
``model_served`` has no absent case to default: the field is required on every record ever
written, historical ones included, so :func:`provenance_of` is a **total** function with no
"unknown" branch to get wrong.

**Which way the remaining failure mode points.** The one way a scripted landing could be
misread as agent-chosen is if the scripted adapter failed to stamp
:data:`SCRIPTED_PROVIDER_NAME`. It cannot: ``DecisionResponse.model_served`` is a required
field of a frozen dataclass, the adapter sets it from :data:`SCRIPTED_MODEL_REF` (a module
constant) on the single ``DecisionResponse`` it ever constructs, ``ProviderChain`` carries it
through ``dataclasses.replace`` without touching it, and
``provider/accounting.py:build_model_call`` copies it verbatim. The opposite error -- some
*other* adapter naming itself ``scripted`` and being excluded from the chosen tier -- would
under-report the agent's demonstrated breadth, which is the visible, recoverable direction.
The name is reserved here for exactly that reason.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from civsim_harness.models.common import ModelRef
from civsim_harness.models.records import ModelCall

__all__ = [
    "SCRIPTED_MODEL_NAME",
    "SCRIPTED_MODEL_REF",
    "SCRIPTED_PROVIDER_NAME",
    "DecisionProvenance",
    "provenance_of",
    "provenance_of_model_ref",
]

#: The reserved ``ModelRef.provider`` value that means "this decision was supplied by a
#: declared script, not chosen by an agent". No adapter other than
#: ``provider/scripted.py`` may report it.
SCRIPTED_PROVIDER_NAME: Final = "scripted"

#: The "model" half of :data:`SCRIPTED_MODEL_REF`. Versioned for the same reason
#: ``provider/stochastic.py``'s is: a recorded run is reproducible *against* the execution
#: policy that produced it, so a future change to how a script is interpreted should be a new
#: name here rather than a silent re-reading of old records.
SCRIPTED_MODEL_NAME: Final = "declared-sequence-v1"

#: What ``provider/scripted.py`` reports as ``DecisionResponse.model_served`` on every call.
SCRIPTED_MODEL_REF: Final = ModelRef(
    provider=SCRIPTED_PROVIDER_NAME, model=SCRIPTED_MODEL_NAME
)


class DecisionProvenance(StrEnum):
    """Where one recorded decision came from.

    Two values, and there is deliberately no third: the field this is derived from cannot be
    absent (see the module docstring), so there is no "unknown" case for a caller to resolve
    and therefore no chance to resolve it in the fabricating direction. *Whether a declared
    script step ever ran at all* is a different question with a different answer -- it is
    answered by the script ledger in ``provider/scripted.py``, which distinguishes a step that
    was issued and refused from one that was never reached, precisely so that absence and
    unobservability do not share a representation.
    """

    #: An agent -- a model, or a sampler standing in for one -- selected this action itself.
    #: This is what the breadth/coverage scorecard has always counted.
    AGENT_CHOSEN = "agent_chosen"
    #: A declared script named this action. The harness still evaluated availability,
    #: normalised the arguments, dispatched, verified and recorded it -- everything except the
    #: choice. Proves the action works; proves nothing about what an agent would pick.
    SCRIPTED = "scripted"


def provenance_of_model_ref(model_served: ModelRef) -> DecisionProvenance:
    """Classify one ``model_served`` reference. Total: every input maps to exactly one value."""
    if model_served.provider == SCRIPTED_PROVIDER_NAME:
        return DecisionProvenance.SCRIPTED
    return DecisionProvenance.AGENT_CHOSEN


def provenance_of(model_call: ModelCall) -> DecisionProvenance:
    """Classify the decision *model_call* served.

    Reads the call's own required ``model_served``. Because
    ``DecisionStepBundle`` refuses to hold a decision whose ``model_call_id`` disagrees with
    the model call beside it, this is the provenance of that bundle's ``Decision`` too --
    established by an invariant that already existed rather than by a second field that could
    drift out of agreement with this one.
    """
    return provenance_of_model_ref(model_call.model_served)
