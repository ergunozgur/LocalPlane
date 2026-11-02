"""Read-only, non-ingesting service diagnosis. No durable service is passed here."""

from localplane.backend.agent_client import AgentError
from localplane.backend.domain.identity import identify_systemd_unit, IdentityBasis
from localplane.protocol.journal import response, validate, valid_service


def read_journal(client, record) -> dict:
    unit = record.identity_value
    if not valid_service(unit):
        return response("unsupported")
    if (record.identity_basis != IdentityBasis.PROVIDER_ID
            or identify_systemd_unit(record.host_id, unit).object_id != record.object_id):
        return response("identity_changed")
    if record.observation is None or record.observation.facts.get("canonical_id") != unit:
        return response("identity_changed")
    try:
        payload = client.systemd_unit_journal(unit, record.host_id)
        if not isinstance(payload, dict) or set(payload) != {"host_id", "journal"}:
            return response("malformed", unit)
        if payload["host_id"] != record.host_id:
            return response("identity_changed", unit)
        journal = payload["journal"]
        if isinstance(journal, dict) and journal.get("canonical_id") not in (None, unit):
            return response("identity_changed", unit)
        return validate(payload["journal"], unit)
    except AgentError as exc:
        status = {"timeout": "timeout", "unsupported_method": "unsupported"}.get(exc.code, "unavailable")
        return response(status, unit)
    except (ValueError, TypeError, KeyError, OverflowError):
        return response("malformed", unit)
    except Exception:
        # No raw peer error/detail, validation input, traceback or MESSAGE in HTTP/logs.
        return response("unavailable", unit)
