"""Platform-owned MCP descriptors; execution remains in application services."""

import copy
from typing import Any

from app.domain.workbench.compute import NAME as COMPUTE_NAME
from app.domain.workbench.data_request import DataRequest

_TASK = {
    "type": "string",
    "description": "The task id given in the step instructions.",
}
_COMPUTE_FIELDS = {
    "name": {"type": "string", "maxLength": 200},
    "expression": {"type": "string", "maxLength": 500},
    "inputs": {
        "type": "object",
        "minProperties": 1,
        "maxProperties": 50,
        "propertyNames": {"pattern": COMPUTE_NAME.pattern},
        "additionalProperties": {
            "anyOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ]
        },
    },
    "round": {"type": "integer", "minimum": 0, "maximum": 10},
}


# Model-facing alternatives must carry their own fields and required lists.
# Required-only oneOf/not fragments survive Codex lowering as empty objects;
# Kimi Responses reproduced {} for single calls. Self-contained anyOf branches
# preserve both existing call shapes and keep mixed calls invalid. The service
# independently validates inputs, ownership, batch limits and evidence refs.
_COMPUTE_SINGLE_SCHEMA = {
    "type": "object",
    "properties": {"task": _TASK, **_COMPUTE_FIELDS},
    "required": ["task", "name", "expression", "inputs"],
    "additionalProperties": False,
}
_COMPUTE_BATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "task": _TASK,
        "batch": {
            "type": "array",
            "minItems": 1,
            "maxItems": 50,
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "pattern": COMPUTE_NAME.pattern},
                    **_COMPUTE_FIELDS,
                },
                "required": ["key", "name", "expression", "inputs"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["task", "batch"],
    "additionalProperties": False,
}

# Reuse the locked data-request/v1 contract instead of hiding its vocabulary and
# period fields behind unconstrained strings/objects in the model-facing tool.
_DATA_REQUEST_SCHEMA = DataRequest.model_json_schema()
_DATA_FIELDS = _DATA_REQUEST_SCHEMA["properties"]


TOOLS: dict[str, dict[str, Any]] = {
    "list_project_conversations": {
        "description": (
            "List the conversations of the project this task belongs to: id, kind "
            "(chat or work), title, when, number of turns and the first message. "
            "The current conversation is marked; it is already in your context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"task": _TASK},
            "required": ["task"],
            "additionalProperties": False,
        },
    },
    "read_project_conversation": {
        "description": (
            "Read one conversation of the project: who said what and what was done. "
            "Commands and file changes are summarised. Paged; ask for the next page "
            "when `pages` is greater than `page`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": _TASK,
                "conversation": {
                    "type": "string",
                    "description": "Id from list_project_conversations.",
                },
                "page": {"type": "integer", "minimum": 1, "default": 1},
            },
            "required": ["task", "conversation"],
            "additionalProperties": False,
        },
    },
    "discover_source_tools": {
        "description": "Discover registered data suppliers available to this task owner. Omit service to list domains, then pass one service to read its current tool input schemas. This does not fetch financial data. Use the exact route and arguments in request_data.source_query; user confirmation is still required. Supplier responses may contain annual report excerpts or links and are not necessarily complete PDFs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": _TASK,
                "service": {"type": "string", "maxLength": 64},
            },
            "required": ["task"],
            "additionalProperties": False,
        },
    },
    "find_subject": {
        "description": (
            "Look up the code of a listed company by its name, short name, pinyin "
            "initials or code (A shares on the Shanghai, Shenzhen and Beijing "
            "exchanges). Returns [{subject, name}], best match first. Use the "
            "subject as the code in request_data; never write a code from memory."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": _TASK,
                "name": {"type": "string", "minLength": 1, "maxLength": 40},
            },
            "required": ["task", "name"],
            "additionalProperties": False,
        },
    },
    "request_data": {
        "description": (
            "Propose the data this task needs (in step 0), or ask for data that is "
            "not pinned to it yet (for example one more year's annual report). It "
            "is listed for the user to confirm, change or drop; you cannot read it "
            "before that and the task pauses after this step. Each item: subject "
            "(a code from find_subject; leave it out for data about no particular "
            "company), doc_type (a type or class from describe_items, e.g. "
            "annual_report, semiannual_report, q1_report, q3_report; "
            "interim_announcement is NOT a periodic report). For explicit dates "
            "use period {kind: fixed, start, end}, including periodic reports; "
            "preserve the user's requested dates instead of replacing them with "
            "latest. Use {kind: latest, count} only for the latest periodic reports, "
            "or {kind: recent, months} for recent non-periodic material. "
            "reading (original, text, pages or table), optional "
            "topics and edition, essential (true if the steps cannot be done "
            "without it) and reason (why it is needed). At most 20 items. "
            "If a value is rejected, consult describe_items and correct that value; "
            "do not drop required data or substitute a different document type "
            "just to pass validation. State any additional baseline period separately. "
            "For financial numbers (revenue, net profit attributable to the parent, "
            "operating cash flow …) ask for the numbers, not a source: subject, "
            "metrics (chart-of-accounts codes from describe_items, e.g. IS.REVENUE), "
            "period {kind: fixed, start, end}, optional consolidation (consolidated or "
            "parent), duration (year, half, quarter, ytd) and disclosure (first = as "
            "originally disclosed, the default; latest = as later restated). Never name "
            "a supplier or channel: the data service decides where and how to get the "
            "numbers. Request report originals separately (doc_type, reading pages) when "
            "the steps must check figures against report pages."
        ),
        "inputSchema": {
            "type": "object",
            "$defs": copy.deepcopy(_DATA_REQUEST_SCHEMA["$defs"]),
            "properties": {
                "task": _TASK,
                "needs": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "subject": {"type": "string", "maxLength": 64},
                            # 要哪些数（SDD 0051）：写了 metrics 就是这一种，不写 doc_type
                            "metrics": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 30,
                                "items": {
                                    "type": "string",
                                    "pattern": r"^[A-Z]{2}\.[A-Z0-9_]{1,60}$",
                                },
                            },
                            "consolidation": {"enum": ["consolidated", "parent"]},
                            "duration": {"enum": ["year", "half", "quarter", "ytd"]},
                            "disclosure": {"enum": ["first", "latest"]},
                            "doc_type": copy.deepcopy(_DATA_FIELDS["doc_type"]),
                            "source_query": copy.deepcopy(_DATA_FIELDS["source_query"]),
                            "period": copy.deepcopy(_DATA_FIELDS["period"]),
                            "reading": {"enum": ["original", "text", "pages", "table"]},
                            "topics": copy.deepcopy(_DATA_FIELDS["topics"]),
                            "edition": {"enum": ["full", "summary", "english"]},
                            "max_count": copy.deepcopy(_DATA_FIELDS["max_count"]),
                            "essential": {"type": "boolean"},
                            "reason": {"type": "string", "maxLength": 500},
                        },
                        "required": ["reason"],
                    },
                },
            },
            "required": ["task", "needs"],
            "additionalProperties": False,
        },
    },
    "find_expert": {
        # Business read only: the immutable directory receipt is audit evidence,
        # not an expert selection, delegation, purchase, or permission change.
        "annotations": {"readOnlyHint": True},
        "description": (
            "List the experts this user can ask on knowmart: knowbot_id, name, "
            "tagline, version, scope {does, does_not, elsewhere} exactly as each "
            "expert wrote it, price, and free (true when asking costs the user "
            "nothing extra). Only public information; never ranked. Match only by "
            "the experts' own scope against the user's question."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"task": _TASK},
            "required": ["task"],
            "additionalProperties": False,
        },
    },
    "compute": {
        "description": (
            "Compute exact decimal results and record them. Every call must include "
            "task copied from the current step instructions; never call with {}. "
            "Use either {task, name, expression, inputs, round?}, "
            "or {task, batch: [{key, name, expression, "
            "inputs, round?}, ...]} for up to 50 related calculations and 500 total "
            "input references. Prefer a batch when facts are already available. "
            "Batch inputs may use batch:key to reference an earlier row; forward "
            "references are refused. Returns {results: [{key, id, value, formula, "
            "inputs}, ...]}; receipts are ordinary compute ids. The entire batch "
            "is refused if any row fails; split unrelated calculations if needed. "
            "Single-call `inputs` maps short "
            "names to fact refs (fact:… or raw:… from query_facts), to the id of an "
            "earlier computation of this task (e.g. c17), or to a list of them. "
            "Input names are case-sensitive ASCII identifiers, 1–32 characters: "
            "start with a letter or _, then letters, digits or _. For example, "
            "inputs {A: fact:…, L: fact:…, E: fact:…} uses expression A - (L + E). "
            "`expression` uses only those names, "
            "numbers, + - * / ( ), ** (fractional exponents require a positive base; "
            "absolute exponent at most 100, e.g. (end / start) ** (1 / years) - 1), "
            "and sum avg median min max abs (these take lists). "
            "You cannot pass numbers as inputs. Returns {id, value, formula, inputs}: "
            "put the id as compute_id next to every computed number you hand back. "
            "A zero denominator is refused: report the figure as not applicable. "
            "Ids are not consecutive (c3, c17, …); that is normal, nothing is missing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_COMPUTE_SINGLE_SCHEMA["properties"],
                **_COMPUTE_BATCH_SCHEMA["properties"],
            },
            "required": ["task"],
            "additionalProperties": False,
            "anyOf": [_COMPUTE_SINGLE_SCHEMA, _COMPUTE_BATCH_SCHEMA],
        },
    },
}
