"""Workflow engine config defaults."""
from __future__ import annotations

from atom.config.schema import AtomConfig, WorkflowConfig


def test_workflow_config_defaults():
    cfg = AtomConfig()
    assert cfg.workflow.max_parallel == 4
    assert cfg.workflow.task_timeout_seconds == 1800


def test_workflow_config_override():
    wc = WorkflowConfig(max_parallel=2, task_timeout_seconds=60)
    assert wc.max_parallel == 2 and wc.task_timeout_seconds == 60


def test_retry_config_defaults():
    from atom.config.schema import AtomConfig
    cfg = AtomConfig()
    assert cfg.retry.max_retries == 20
    assert cfg.retry.base_delay == 1.0
    assert cfg.retry.max_delay == 30.0
    assert cfg.retry.jitter is True


def test_retry_config_override():
    from atom.config.schema import RetryConfig
    rc = RetryConfig(max_retries=5, base_delay=0.5, max_delay=10.0, jitter=False)
    assert rc.max_retries == 5 and rc.base_delay == 0.5
    assert rc.max_delay == 10.0 and rc.jitter is False


def test_queue_config_defaults():
    from atom.config.schema import AtomConfig
    cfg = AtomConfig()
    assert cfg.queue.max_concurrent_runs == 1
    assert cfg.queue.poll_interval_seconds == 3.0


def test_queue_config_override():
    from atom.config.schema import QueueConfig
    qc = QueueConfig(max_concurrent_runs=3, poll_interval_seconds=0.5)
    assert qc.max_concurrent_runs == 3 and qc.poll_interval_seconds == 0.5
