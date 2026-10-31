// 群聊页「备注 + 老板」下拉框：不连微信，只验证渲染和待保存状态。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../webui/app.js'), 'utf8');
const region = (start, end) => source.slice(source.indexOf(start), source.indexOf(end));
const code = region('const lines = value =>', 'async function save(data, message)')
  + region("$('#discovered-chats').onclick=", 'const roleLabels = {');

function fixture(handler) {
  const nodes = new Map();
  const $ = key => {
    if (!nodes.has(key)) nodes.set(key, {value: '', textContent: '', innerHTML: '', disabled: false, handlers: {},
      addEventListener(type, callback) { this.handlers[type] = callback; },
      classList: {toggle() {}, add() {}, remove() {}}, querySelectorAll() { return []; }});
    return nodes.get(key);
  };
  // 页面里 #group-ids 就是 #groups-form 的 groups 字段，这里把两个 stub 接成同一个对象。
  $('#groups-form').elements = {groups: $('#group-ids')};
  const scope = {$,
    api: handler,
    action: async callback => { await callback(); },
    toast: () => {},
    escape: s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])),
    roleLabels: {owner: '群主', admin: '群管理员', member: '普通群友', unknown: '身份未知'},
    config: null, groupNameDraft: new Map(), ownerDraft: new Map(), groupMemberCache: new Map(), discoveredGroups: []};
  vm.createContext(scope);
  vm.runInContext(code, scope);
  return {scope, $};
}

test('已保存的老板在群列表里回显，成员没拉到也不会显示成未指定', () => {
  const {scope, $} = fixture(async () => { throw Error('不应请求'); });
  scope.config = {groups: ['123@chatroom'], group_names: {}, owner_ids: {'123@chatroom': ['wxid_boss']}};
  scope.ownerDraft = new Map([['123@chatroom', 'wxid_boss']]);
  $('#group-ids').value = '123@chatroom';
  scope.renderGroupNames();
  const html = $('#group-name-list').innerHTML;
  assert.match(html, /data-group-owner="123@chatroom"/);
  assert.match(html, /<option value="wxid_boss" selected>wxid_boss<\/option>/);
});

test('拉群成员后下拉框带昵称和角色，选中即写入待保存的老板', async () => {
  const {scope, $} = fixture(async endpoint => {
    assert.match(endpoint, /^group-members\?room=/);
    return {members: [{wxid: 'wxid_boss', nickname: '老板', role: 'owner'},
                      {wxid: 'wxid_peer', nickname: '群友', role: 'member'}]};
  });
  scope.config = {groups: ['123@chatroom'], group_names: {}, owner_ids: {}};
  $('#group-ids').value = '123@chatroom';
  scope.renderGroupNames();
  assert.equal(await scope.loadGroupMembers('123@chatroom'), 2);
  const html = $('#group-name-list').innerHTML;
  assert.match(html, /老板（群主）/);
  assert.match(html, /群友（普通群友）/);
  $('#group-name-list').handlers.change({target: {closest: () => ({dataset: {groupOwner: '123@chatroom'}, value: 'wxid_boss'})}});
  assert.equal(scope.ownerDraft.get('123@chatroom'), 'wxid_boss');
});

test('管理台版本不带 owner_ids 时给出提示并禁用下拉框', () => {
  const {scope, $} = fixture(async () => { throw Error('不应请求'); });
  scope.config = {groups: ['123@chatroom'], group_names: {}};
  $('#group-ids').value = '123@chatroom';
  scope.renderGroupNames();
  const html = $('#group-name-list').innerHTML;
  assert.match(html, /请重启管理台后指定每个群的老板/);
  assert.match(html, /data-group-owner="123@chatroom" disabled/);
});

test('点已发现的群会填入群列表并自动拉群成员', async () => {
  let requested = '';
  const {scope, $} = fixture(async endpoint => {
    requested = endpoint;
    return {members: [{wxid: 'wxid_boss', nickname: '老板', role: 'owner'}]};
  });
  scope.config = {groups: [], group_names: {}, owner_ids: {}};
  $('#discovered-chats').onclick({target: {closest: () => ({dataset: {room: '789@chatroom'}})}});
  await new Promise(resolve => setImmediate(resolve));
  assert.match($('#groups-form').elements.groups.value, /789@chatroom/);
  assert.match(requested, /789%40chatroom/);
  assert.match($('#group-name-list').innerHTML, /老板（群主）/);
});
