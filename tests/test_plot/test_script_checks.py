"""
Tests the checks for utilities

For debug information use:
pytest-3 --log-cli-level debug

"""

import pytest
import coverage
from . import context   # noqa: F401
from kibot.misc import (MISSING_TOOL, CMD_EESCHEMA_DO)
from kibot.kiplot import (check_script, check_version)
import kibot.kiplot


cov = coverage.Coverage()


def test_check_script(caplog):
    cov.load()
    cov.start()
    with pytest.raises(SystemExit) as pytest_wrapped_e:
        check_script('bogus', '')
    cov.stop()
    cov.save()
    assert pytest_wrapped_e.type == SystemExit
    assert pytest_wrapped_e.value.code == MISSING_TOOL
    assert "No `bogus` command found" in caplog.text


def test_check_version_1(caplog):
    cov.load()
    cov.start()
    with pytest.raises(SystemExit) as pytest_wrapped_e:
        check_version('ls', '1.1.1')
    cov.stop()
    cov.save()
    assert pytest_wrapped_e.type == SystemExit
    assert pytest_wrapped_e.value.code == MISSING_TOOL
    assert "Unable to determine ls version" in caplog.text


def test_check_version_2(caplog):
    # Avoid interaction with other tests: reset the versions cache
    kibot.kiplot.script_versions = {}
    cov.load()
    cov.start()
    with pytest.raises(SystemExit) as pytest_wrapped_e:
        check_version(CMD_EESCHEMA_DO, '20.1.1')
    cov.stop()
    cov.save()
    assert pytest_wrapped_e.type == SystemExit
    assert pytest_wrapped_e.value.code == MISSING_TOOL
    assert "Wrong version for `eeschema_do`" in caplog.text
