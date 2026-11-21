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
            "Propose 1–20 needs for this task. The user may change or drop them; wait for "
            "confirmation before collection or reading. Give reason, essential, and subject "
            "from find_subject (omit for non-company data). For documents use doc_type from "
            "describe_items, reading, optional topics/edition. For exact dates use period "
            "{kind: fixed, start, end}; latest/count is for latest periodic reports, recent/months "
            "for recent non-periodic material. interim_announcement is not a periodic report. "
            "Request baseline periods separately. For figures specify metrics from describe_items "
            "(e.g. IS.REVENUE), fixed period, optional consolidation, duration and disclosure "
            "(first by default, latest for restated). The service selects the supplier for metrics. "
            "Request report pages separately for source checks. For a specific supplier document "
            "query use the discovered source_query route and arguments. Correct rejected fields "
            "with describe_items; keep the requested data and document type."
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
            "Compute exact decimals and record results in the active task. Always copy task from "
            "the step instructions. For one calculation send task, name, expression, inputs, "
            "optional round. For a batch send task and batch (up to 50 rows, 500 input refs total). "
            "Each row also needs a unique key. batch:key names an earlier row; the batch is atomic. "
            "Inputs map case-sensitive ASCII identifiers (1–32 chars; start with a letter or _) "
            "to fact:… or raw:… references, earlier c<id> receipts from this task, or lists of refs. "
            "Never supply input values directly. Expressions support numbers, input names, parentheses, "
            "+ - * / ** and sum, avg, median, min, max, abs. Fractional powers require a positive base; "
            "absolute exponent at most 100. Report zero denominators as not applicable. Return each "
            "computed value with its compute_id from the receipt. Batch results are in results, "
            "with one key and receipt per row."
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
