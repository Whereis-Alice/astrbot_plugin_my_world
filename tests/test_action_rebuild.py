"""Destructive-scope boundaries of map-preserving activity regeneration."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node is required for the editor's pure state tests")
class ActionRebuildTests(unittest.TestCase):
    def test_replacement_is_scoped_complete_and_preserves_shared_actions(self):
        module = (Path(__file__).resolve().parents[1] / "pages/world_editor/action-rebuild.js").as_uri()
        script = "import {actionRebuildPlan} from " + repr(module) + ";\n" + r'''
import assert from 'node:assert/strict';
const config = {world: {nodes: [{id:'a', name:'卧室', zone_id:'home'}, {id:'b', name:'厨房', zone_id:'home'}, {id:'c', name:'公园', zone_id:'outside'}],
  zones:[{id:'home', name:'家'}, {id:'outside', name:'外面'}], edges:[{from:'a',to:'b'}],
  sleep:{recovery_action_id:'old'}, actions:[
    {id:'old', scope:'node', allowed_nodes:['a'], group:'旧组'},
    {id:'shared', scope:'node', allowed_nodes:['a','c'], group:'共享组'},
    {id:'outside', scope:'node', allowed_nodes:['c'], group:'外部'},
    {id:'global', scope:'global', allowed_nodes:[]},
    {id:'sleep', builtin:true, scope:'node', allowed_nodes:['a']}]},
  schedules:{schedules:[{id:'d',enabled:true,action_chain:[{type:'old'}]}]}};
const original = structuredClone(config), draft = {id:'new', scope:'node', allowed_nodes:['a'], group:'model_group'};
const p = actionRebuildPlan(config, ['a'], [draft]);
assert.deepEqual(config, original);
assert.deepEqual(p.config.world.nodes, original.world.nodes);
assert.deepEqual(p.config.world.edges, original.world.edges);
assert.deepEqual(p.config.world.zones, original.world.zones);
assert.deepEqual(p.removed.map(a=>a.id), ['old']);
assert.deepEqual(p.config.world.actions.find(a=>a.id==='shared').allowed_nodes, ['c']);
assert.deepEqual(p.config.world.actions.find(a=>a.id==='outside'), original.world.actions[2]);
assert.equal(p.added[0].group, '卧室'); assert.equal(p.config.schedules.schedules[0].enabled, false);
assert.equal(p.config.world.sleep.recovery_action_id, ''); assert.deepEqual(p.removedGroups, ['旧组']);
assert.equal(actionRebuildPlan(config, ['a'], [draft], 'zone').added[0].group, '家');
assert.throws(()=>actionRebuildPlan(config,['a','b'],[draft]), /至少保留一个/);
assert.throws(()=>actionRebuildPlan(config,['a'],[{...draft, allowed_nodes:['c']}]), /范围/);
assert.throws(()=>actionRebuildPlan(config,['a'],[{...draft, _pick:false}]), /至少保留一个/);
assert.equal(actionRebuildPlan(config,['a'],[{...draft,id:'global'}]).added[0].id,'global_2');
const all = actionRebuildPlan(config, ['a','b','c'], ['a','b','c'].map(id=>({...draft,id:'new_'+id,allowed_nodes:[id]})));
assert.deepEqual(all.removed.map(a=>a.id), ['old','shared','outside']);
assert(all.config.world.actions.some(a=>a.id==='global')); assert(all.config.world.actions.some(a=>a.id==='sleep'));
console.log('scope, shared bindings, map preservation, groups, references, empty coverage: passed');
'''
        result = subprocess.run([shutil.which("node"), "--input-type=module"], input=script, text=True,
            encoding="utf-8", capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
