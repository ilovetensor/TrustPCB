import asyncio

import pytest

from trustpcb import runners
from trustpcb.runners.base import RunRequest, call_tool, result_payload, tool_specs
from trustpcb.runners.scripted import ScriptedRunner


class Recorder:
    def __init__(self):
        self.events = []

    async def emit(self, kind, payload):
        self.events.append((kind, payload))

    async def interact(self, kind, payload):
        self.events.append((kind, payload))
        return {'allow': True}

    def kinds(self):
        return [kind for kind, _ in self.events]


def request(tools=None, script=None, max_turns=10, text='hi'):
    return RunRequest(None, 'sys', text, tools, max_turns, {'script': script} if script is not None else {})


@pytest.fixture
def registry(fakes):
    async def measure(args):
        return fakes.Result(text=f"gap {args['a']} mm", structured={'gap': args['a']})

    async def snap(args):
        return fakes.Result(text='view', images=[{'mime': 'image/png', 'data_b64': 'AAAA'}])

    async def boom(args):
        raise RuntimeError('tool crashed')

    return fakes.Registry(
        fakes.Spec('measure', 'measure', {'type': 'object'}, measure),
        fakes.Spec('snap', 'image', {'type': 'object'}, snap),
        fakes.Spec('boom', 'fails', {'type': 'object'}, boom),
    )


async def test_default_script_emits_text_session_and_usage():
    sink = Recorder()
    await ScriptedRunner().run(request(), sink.emit, sink.interact)
    assert sink.kinds()[0] == 'session' and 'text_delta' in sink.kinds() and sink.kinds()[-1] == 'usage'


async def test_script_calls_tools_in_order_and_reports_results(registry):
    sink = Recorder()
    script = [{'thought': 'look'}, {'tool': 'measure', 'args': {'a': 0.3}}, {'tool': 'snap'}, {'tool': 'boom'}, {'tool': 'missing'}]
    await ScriptedRunner(script).run(request(registry, max_turns=9), sink.emit, sink.interact)
    results = [p for k, p in sink.events if k == 'tool_result']
    assert results[0]['content'] == 'gap 0.3 mm' and results[0]['structured'] == {'gap': 0.3} and results[0]['is_error'] is False
    assert results[1]['images'] == [{'mime': 'image/png', 'bytes_b64': 4}]  # image bytes are never inlined in events
    assert results[2]['is_error'] and 'tool crashed' in results[2]['content']
    assert results[3]['is_error'] and 'Unknown tool' in results[3]['content']
    assert [p['name'] for k, p in sink.events if k == 'tool_start'] == ['measure', 'snap', 'boom', 'missing']
    assert 'thought' in sink.kinds()


async def test_max_turns_stops_cleanly_with_a_status_event(registry):
    sink = Recorder()
    script = [{'tool': 'measure', 'args': {'a': 1}}, {'tool': 'measure', 'args': {'a': 2}}, {'text': 'not reached'}]
    await ScriptedRunner(script).run(request(registry, max_turns=1), sink.emit, sink.interact)
    assert sink.kinds().count('tool_start') == 1
    assert ('status', {'state': 'max_turns', 'max_turns': 1}) in sink.events
    assert 'text_delta' not in sink.kinds()


async def test_interact_branches_on_the_answer():
    sink = Recorder()
    script = [{'interact': 'test', 'payload': {'title': 't'}},
              {'if_answer': {'allow': True}, 'then': [{'text': 'ran'}], 'else': [{'text': 'declined'}]}]
    await ScriptedRunner(script).run(request(), sink.emit, sink.interact)
    assert ''.join(p['text'] for k, p in sink.events if k == 'text_delta') == 'ran'


async def test_failure_step_raises_and_cancel_interrupts_sleep():
    sink = Recorder()
    with pytest.raises(RuntimeError, match='scripted failure'):
        await ScriptedRunner([{'fail': 'scripted failure'}]).run(request(), sink.emit, sink.interact)
    runner = ScriptedRunner([{'sleep': 30}, {'text': 'late'}])
    task = asyncio.create_task(runner.run(request(), sink.emit, sink.interact))
    await asyncio.sleep(0.05)
    await runner.cancel()
    await asyncio.wait_for(task, 2)
    assert 'late' not in str(sink.events)


async def test_script_from_environment_file(tmp_path, monkeypatch):
    path = tmp_path / 'script.json'
    path.write_text('[{"text": "from file"}]')
    monkeypatch.setenv('TRUSTPCB_SCRIPT', str(path))
    sink = Recorder()
    await ScriptedRunner().run(request(), sink.emit, sink.interact)
    assert ''.join(p['text'] for k, p in sink.events if k == 'text_delta') == 'from file'


async def test_unknown_script_step_is_an_error():
    with pytest.raises(ValueError):
        await ScriptedRunner([{'bogus': 1}]).run(request(), Recorder().emit, Recorder().interact)


def test_tool_specs_accepts_the_registry_shapes(fakes, registry):
    spec = registry.specs()[0]
    assert tool_specs(registry)[0] is spec
    assert tool_specs([spec]) == [spec]
    assert tool_specs(type('R', (), {'tools': {'measure': spec}})()) == [spec]
    assert tool_specs(type('R', (), {'tools': [spec]})()) == [spec]
    with pytest.raises(TypeError):
        tool_specs(object())


async def test_call_tool_unknown_name(registry):
    with pytest.raises(KeyError):
        await call_tool(registry, 'nope', {})


def test_result_payload_shape(fakes):
    payload = result_payload(fakes.Result(text='x', is_error=True))
    assert payload == {'content': 'x', 'is_error': True, 'images': []}


def test_resolve_profile_default_follows_environment(monkeypatch):
    assert runners.resolve_profile()['name'] == 'scripted'  # conftest sets TRUSTPCB_RUNNER=scripted
    monkeypatch.delenv('TRUSTPCB_RUNNER')
    monkeypatch.delenv('TRUSTPCB_RUNNER_MODULE', raising=False)
    assert runners.resolve_profile()['name'] == 'scripted'
    with pytest.raises(ValueError):
        runners.resolve_profile('nonsense')


def test_load_runner_scripted_and_no_silent_provider_fallback():
    assert isinstance(runners.load_runner('scripted'), ScriptedRunner)
    assert isinstance(runners.load_runner({'name': 'scripted'}), ScriptedRunner)
    assert isinstance(runners.load_runner(None), ScriptedRunner)
    if not runners.available('muse_api'):
        with pytest.raises(RuntimeError, match='not installed'):
            runners.load_runner('muse_api')


def test_profiles_never_name_a_model_family_other_than_muse():
    assert runners.PROFILES['muse_api']['model'] == 'muse-spark-1.3'
    assert set(runners.PROFILES) == {'muse_api', 'scripted'}   # no other runner is named in tracked code


@pytest.fixture
def extension(tmp_path, monkeypatch):
    """A runner module outside the package, selected through TRUSTPCB_RUNNER_MODULE."""
    (tmp_path / 'my_ext_runner.py').write_text(
        "PROFILE = {'model': 'm-1', 'effort': 'low'}\n"
        "class Runner:\n"
        "    def __init__(self, profile): self.profile = profile\n"
        "def create_runner(profile=None): return Runner(profile)\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv('TRUSTPCB_RUNNER')
    monkeypatch.setenv('TRUSTPCB_RUNNER_MODULE', 'my_ext_runner')
    return 'my_ext_runner'


def test_extension_module_becomes_the_default_runner(extension):
    profile = runners.resolve_profile()
    assert profile['name'] == 'my_ext_runner' and profile['runner'] == 'my_ext_runner'
    assert profile['model'] == 'm-1' and profile['effort'] == 'low'      # the module's PROFILE fills these in
    assert profile['static_tool_list'] is True and 'my_ext_runner' in profile['description']
    assert runners.available('my_ext_runner') and 'my_ext_runner' in runners.profile_names()
    runner = runners.load_runner(profile)
    assert runner.profile == profile
    assert runners.load_runner('my_ext_runner').profile['name'] == 'my_ext_runner'    # a review stored the name only


def test_extension_is_not_used_when_another_runner_is_chosen(extension, monkeypatch):
    monkeypatch.setenv('TRUSTPCB_RUNNER', 'scripted')
    assert runners.resolve_profile()['name'] == 'scripted'
    assert isinstance(runners.load_runner(None), ScriptedRunner)
    assert runners.resolve_profile('my_ext_runner')['name'] == 'my_ext_runner'


def test_extension_that_is_missing_or_malformed_is_an_error_not_a_fallback(monkeypatch):
    monkeypatch.delenv('TRUSTPCB_RUNNER')
    monkeypatch.setenv('TRUSTPCB_RUNNER_MODULE', 'no_such_runner_module')
    assert not runners.available('no_such_runner_module')
    with pytest.raises(RuntimeError, match='not installed'):
        runners.resolve_profile()
    monkeypatch.setenv('TRUSTPCB_RUNNER_MODULE', 'not a module; rm -rf')
    with pytest.raises(RuntimeError, match='dotted Python module name'):
        runners.resolve_profile()
    assert runners.profile_names() == list(runners.PROFILES)
    monkeypatch.setenv('TRUSTPCB_RUNNER_MODULE', 'somewhere.scripted')
    with pytest.raises(RuntimeError, match='reserved'):
        runners.resolve_profile()


def test_extension_name_is_unknown_when_the_setting_is_absent(monkeypatch):
    monkeypatch.delenv('TRUSTPCB_RUNNER_MODULE', raising=False)
    with pytest.raises(ValueError, match='Unsupported'):
        runners.resolve_profile('my_ext_runner')
