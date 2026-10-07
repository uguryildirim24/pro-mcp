import time

import pytest

from pro_mcp import commands


def finish(result):
    deadline = time.monotonic() + 5
    output = result['output']
    while not result['finished']:
        assert time.monotonic() < deadline
        result = commands.interact(result['session_id'], yield_time_ms=100)
        output += result['output']
    return result, output


def test_command_can_write_read_and_test_in_selected_directory(tmp_path):
    result, output = finish(commands.execute(
        "printf 'hello' > sample.txt; test \"$(cat sample.txt)\" = hello; cat sample.txt",
        str(tmp_path), 1000))
    assert result['exit_code'] == 0
    assert output == 'hello'
    assert (tmp_path / 'sample.txt').read_text() == 'hello'


def test_long_command_input_and_incremental_output(tmp_path):
    result = commands.execute("printf ready; read line; printf ':%s' \"$line\"", str(tmp_path), 30)
    assert not result['finished']
    prefix = result['output']
    result, output = finish(commands.interact(result['session_id'], 'received\n', 1000))
    assert prefix + output == 'ready:received'
    assert result['exit_code'] == 0
    with pytest.raises(ValueError, match='completed'):
        commands.interact(result['session_id'])


def test_stop_process_group(tmp_path):
    result = commands.execute('sleep 30', str(tmp_path), 0)
    result, _ = finish(commands.interact(result['session_id'], terminate=True))
    assert result['exit_code'] != 0


def test_failed_command_and_invalid_directory(tmp_path):
    result, output = finish(commands.execute('echo failure >&2; exit 7', str(tmp_path)))
    assert result['exit_code'] == 7 and 'failure' in output
    with pytest.raises(ValueError, match='directory'):
        commands.execute('true', str(tmp_path / 'missing'))


def test_output_is_bounded(tmp_path):
    result, output = finish(commands.execute("head -c 1100000 /dev/zero", str(tmp_path)))
    assert len(output) <= commands.MAX_OUTPUT
    assert result.get('truncated_bytes', 0) >= 100000
