"""Synthetic frozen method permission; no external service is called."""

ACCESS = {
    "requirement_id": "deliver-notice",
    "capability_ref": "synthetic-mail",
    "version": "1",
    "operations": ["send"],
    "resources": [
        {
            "resource_kind": "connector",
            "resource_role": "recipient",
            "values": ["test@example.test"],
        }
    ],
    "data_transfer": {
        "destination": "https://tools.example.test/mcp",
        "argument_fields": ["to"],
        "purpose": "Deliver the confirmed notice",
    },
    "required_when": "always",
    "on_unavailable": "pause",
}
