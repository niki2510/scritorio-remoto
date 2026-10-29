import app as server


def test_agent_is_resolved_by_persistent_uid_after_reconnect(monkeypatch):
    monkeypatch.setattr(server, "_agent_sids", {"new-sid"})
    monkeypatch.setattr(server, "_devices", {
        "new-sid": {"device_uid": "same-machine"},
    })

    assert server._resolve_agent_sid_locked("old-sid", "same-machine") == "new-sid"


def test_active_job_is_rebound_to_reconnected_agent(monkeypatch):
    jobs = {
        "active": {"agent": "old-sid", "agent_uid": "same-machine"},
        "other": {"agent": "other-sid", "agent_uid": "other-machine"},
    }
    monkeypatch.setattr(server, "_ftp_agent_jobs", jobs)

    server._rebind_agent_jobs_locked("new-sid", "same-machine")

    assert jobs["active"]["agent"] == "new-sid"
    assert jobs["other"]["agent"] == "other-sid"
