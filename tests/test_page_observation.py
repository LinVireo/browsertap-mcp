"""Actionable observations and caller guidance through the public MCP surface."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from test_frame_locators import run_frames
from test_mcp_result_regressions import _wire_call
from test_page_scripts import _run_node

from browsertap_mcp import server as S
from browsertap_mcp import simphtml
from browsertap_mcp.page_input import normalize_locator


def _snapshot():
    return {
        '__btap_observation_payload__': True,
        'page': '<body><label>Name <input id="name"></label></body>',
        'groups': [],
        'observation': {
            'status': 'success', 'targets': [
                {'locator': {'css': '#name'}, 'recommended_tool': 'page_type', 'editable': True},
            ],
            'frames': [{'frame': [{'css': '#child'}], 'inspected': False}],
            'truncated': False,
        },
    }


@pytest.fixture
def scan_driver(monkeypatch):
    driver = SimpleNamespace(default_session_id='browser:old', calls=[])

    def switch(session_id=None):
        driver.default_session_id = session_id or 'browser:7'

    def execute(script, **kwargs):
        driver.calls.append(('js', script, kwargs))
        return {'data': _snapshot()}

    def ext(payload, **kwargs):
        driver.calls.append(('frame', payload, kwargs))
        return {'data': {'met': True, 'locator_status': 'found', 'scan': _snapshot()}}

    driver.execute_js = execute
    driver.ext_cmd = ext
    monkeypatch.setattr(S, 'require_driver', lambda: driver)
    monkeypatch.setattr(S, 'ensure_sessions', lambda **kwargs: [{'id': 'browser:7'}])
    monkeypatch.setattr(S, 'switch_session', switch)
    monkeypatch.setattr(S, 'compact_tabs', lambda: [])
    monkeypatch.setattr(S, '_page_render_state', lambda *args: None)
    return driver


def test_scan_exposes_actionable_targets_through_mcp(scan_driver):
    result = _wire_call(S.mcp, 'scan_page', {'session_id': 'browser:7'})
    assert result.isError is False
    observation = result.structuredContent['data']['observation']
    assert observation['targets'][0]['locator'] == {'css': '#name'}
    assert observation['targets'][0]['recommended_tool'] == 'page_type'
    assert observation['frame'] == []
    assert scan_driver.default_session_id == 'browser:old'
    assert len(scan_driver.calls) == 1


def test_frame_scan_binds_followup_locators_and_never_probes_the_parent(scan_driver):
    result = S.scan_page(session_id='browser:7', frame=['#outer'], max_targets=12)
    observation = result['observation']
    assert observation['targets'][0]['locator'] == {'css': '#name', 'frame': [{'css': '#outer'}]}
    assert observation['frames'][0]['frame'] == [{'css': '#outer'}, {'css': '#child'}]
    assert 'render_state' not in result
    assert scan_driver.default_session_id == 'browser:old'
    assert len(scan_driver.calls) == 1
    kind, payload, routing = scan_driver.calls[0]
    assert kind == 'frame' and payload['action'] == 'query'
    assert payload['tabId'] == 7 and routing['client_id'] == 'browser'
    assert not payload.get('commands')
    assert 'pageTargets(12)' in payload['inspect']


def test_scan_metadata_and_readiness_follow_an_implicitly_rebound_tab(scan_driver, monkeypatch):
    def rebound(script, **kwargs):
        scan_driver.default_session_id = 'browser:8'
        return {'data': _snapshot()}

    probes = []
    scan_driver.execute_js = rebound
    monkeypatch.setattr(S, '_page_render_state', lambda driver, session, timeout: probes.append(session))
    result = S.scan_page()
    assert result['active_session_id'] == 'browser:8'
    assert scan_driver.default_session_id == 'browser:8'
    assert probes == ['browser:8']


def test_frame_file_inputs_do_not_recommend_the_top_document_upload_tool(scan_driver):
    snapshot = _snapshot()
    snapshot['observation']['targets'][0].update(recommended_tool='upload_files', type='file')
    scan_driver.ext_cmd = lambda *args, **kwargs: {
        'data': {'met': True, 'locator_status': 'found', 'scan': snapshot},
    }
    target = S.scan_page(frame=['#outer'])['observation']['targets'][0]
    assert target['recommended_tool'] is None
    assert target['reason'] == 'file_input_frame_unsupported'
    assert normalize_locator(target['locator']) == {'css': '#name', 'frame': [{'css': '#outer'}]}


@pytest.mark.parametrize('reply,status', [
    ({'met': True, 'locator_status': 'found'}, 'frame_scan_unavailable'),
    ({'met': False, 'locator_status': 'ambiguous'}, 'ambiguous'),
    ({'met': False, 'locator_status': 'stale_frame'}, 'stale_frame'),
])
def test_frame_scan_failure_never_becomes_an_empty_success(scan_driver, reply, status):
    scan_driver.ext_cmd = lambda *args, **kwargs: {'data': reply}
    result = _wire_call(S.mcp, 'scan_page', {'session_id': 'browser:7', 'frame': ['#outer']})
    assert result.isError is True
    assert result.structuredContent['error_code'] == status
    assert scan_driver.default_session_id == 'browser:old'


@pytest.mark.parametrize('arguments', [
    {'max_targets': -1}, {'max_targets': 201}, {'max_targets': True},
    {'frame': []}, {'frame': ['#outer'], 'extra_js': 'return 1'},
])
def test_invalid_scan_options_do_not_reach_a_browser(scan_driver, arguments):
    with pytest.raises(ValueError):
        S.scan_page(**arguments)
    assert not scan_driver.calls


def test_observation_preserves_content_budget_and_links(scan_driver):
    result = S.scan_page(session_id='browser:7', maxchars=20)
    assert len(result['content']) <= 20
    assert result['observation']['targets'][0]['editable'] is True
    with pytest.raises(simphtml.PageUnavailable):
        simphtml.unpack_observation({'page': '<body></body>'})


def test_nested_locator_metadata_survives_the_js_value_depth_limit():
    payload = _snapshot()
    locator = {'css': '#input', 'shadow': ['#outer', '#inner']}
    child_frame = {'css': '#child', 'shadow': ['#outer', '#inner']}
    payload['observation']['targets'][0]['locator'] = locator
    payload['observation']['frames'][0]['frame'] = [child_frame]
    payload['observation'] = json.dumps(payload['observation'])
    _, _, observation = simphtml.unpack_observation(payload)
    assert observation['targets'][0]['locator'] == locator
    assert normalize_locator(observation['targets'][0]['locator']) == locator
    assert normalize_locator({'css': 'html', 'frame': observation['frames'][0]['frame']}) == {
        'css': 'html', 'frame': [child_frame],
    }
    payload['observation'] = '{invalid'
    with pytest.raises(simphtml.PageUnavailable):
        simphtml.unpack_observation(payload)


@pytest.mark.parametrize('mode', ['same_process', 'oopif', 'nested'])
def test_frame_query_returns_observation_and_releases_every_lease(mode):
    result = run_frames(mode, 'query', inspect="({found:true,status:'found',scan:{page:document.title}})")
    assert result['result']['data']['scan'] == {'page': 'Generic iframe'}
    assert not result['inputEvents']
    assert result['attached'] == result['detached']
    assert result['objectCount'] == 0


def test_caller_guides_are_discoverable_without_loading_skills():
    async def read_guides():
        resources = {str(resource.uri): resource for resource in await S.mcp.list_resources()}
        for uri, name in [
            ('browsertap://agent/workflow', 'browsertap-default'),
            ('browsertap://agent/recovery', 'browsertap-bridge-recovery'),
        ]:
            assert resources[uri].mimeType == 'text/markdown'
            contents = list(await S.mcp.read_resource(uri))
            assert contents[0].content == (S.ROOT / 'skills' / name / 'SKILL.md').read_text(encoding='utf-8')
        tools = await S.mcp.list_tools()
        assert len(tools) == 51
        scan = next(tool for tool in tools if tool.name == 'scan_page')
        assert scan.inputSchema['properties']['max_targets']['default'] == 80
        assert 'frame' in scan.inputSchema['properties']

    asyncio.run(read_guides())
    instructions = S.mcp._mcp_server.create_initialization_options().instructions
    assert 'browsertap://agent/workflow' in instructions
    assert 'retry_safe' in instructions
    assert 'recommended_tool' in instructions


def test_page_targets_choose_input_methods_without_mutating_dom():
    harness = r"""
const nodes = [];
const document = {
  body: {}, title: 'Fixture', readyState: 'complete',
  nodes,
  querySelectorAll: s => s === '*' ? nodes : nodes.filter(n => s === '#' + n.id),
  getElementById: id => nodes.find(n => n.id === id),
};
const location = {href:'https://example.invalid/'};
const innerWidth = 800, innerHeight = 600, devicePixelRatio = 1;
const CSS = {escape: s => s};
function add(id, tag, attrs = {}, root = document) {
  const node = {
    id, localName:tag, nodeType:1, type:attrs.type || 'text', readOnly:!!attrs.readonly,
    textContent:attrs.label || '', labels:[], isContentEditable:!!attrs.editable,
    getRootNode: () => root,
    getAttribute: name => attrs[name] ?? null,
    matches: selector => selector.includes(',') ||
      (selector === ':disabled' && !!attrs.disabled) ||
      (selector === '[inert]' && !!attrs.inert),
    checkVisibility: () => !attrs.hidden,
    getBoundingClientRect: () => ({left:0,top:0,width:attrs.zero ? 0 : 80,height:20}),
    setAttribute() {throw Error('live DOM mutation');},
    focus() {throw Error('focus mutation');},
  };
  root.nodes.push(node);
  return node;
}
function shadow(host) {
  const children = [];
  host.shadowRoot = {
    host, nodes: children,
    querySelectorAll: s => s === '*' ? children : children.filter(n => s === '#' + n.id),
  };
  return host.shadowRoot;
}
add('name','input',{'aria-label':'Name'});
add('locked','input',{readonly:true});
add('disabled','button',{disabled:true});
add('inert','input',{inert:true});
add('hidden','input',{hidden:true});
add('file','input',{hidden:true,type:'file'});
add('select','select');
add('canvas','canvas');
add('proxy','button',{zero:true});
add('submit','button');
add('frame','iframe');
const outer = shadow(add('outer','div'));
const inner = shadow(add('inner','div',{},outer));
add('shadow-name','input',{},inner);
add('shadow-file','input',{hidden:true,type:'file'},inner);
add('shadow-frame','iframe',{},inner);
PAYLOAD
const all = pageTargets(80);
console.log(JSON.stringify({all, limited:pageTargets(2), disabled:pageTargets(0)}));
"""
    result = _run_node(harness.replace('PAYLOAD', simphtml.js_page_outline))
    targets = {item['locator']['css']: item for item in result['all']['targets']}
    assert targets['#name']['recommended_tool'] == 'page_type'
    assert targets['#name']['name'] == 'Name'
    for target in ['#locked', '#disabled', '#inert', '#select']:
        assert targets[target]['recommended_tool'] is None
        assert targets[target]['editable'] is False
    assert '#hidden' not in targets
    assert targets['#file']['recommended_tool'] == 'upload_files'
    assert targets['#shadow-file']['recommended_tool'] is None
    assert targets['#shadow-file']['reason'] == 'file_input_shadow_unsupported'
    assert normalize_locator(targets['#shadow-name']['locator']) == {
        'css': '#shadow-name', 'shadow': ['#outer', '#inner'],
    }
    assert targets['#submit']['recommended_tool'] == 'page_click'
    for target in ['#canvas', '#proxy']:
        assert targets[target]['reason'] == 'verify_coordinate_target'
        assert targets[target]['recommended_tool'] == 'capture_page_screenshot'
    assert result['all']['frames'][0]['frame'] == [{'css': '#frame'}]
    assert normalize_locator({'css': 'html', 'frame': result['all']['frames'][1]['frame']}) == {
        'css': 'html', 'frame': [{'css': '#shadow-frame', 'shadow': ['#outer', '#inner']}],
    }
    assert len(result['limited']['targets']) == 2 and result['limited']['truncated']
    assert result['disabled']['status'] == 'disabled'
    assert result['disabled']['targets'] == []
