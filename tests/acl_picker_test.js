/* 受限資料夾 picker 的資料處理（樹、搜尋、篩選、授權名單）。

     node tests/acl_picker_test.js

   不另外抄一份邏輯：直接把 admin.js 裡 @@folder-tree-begin 到 @@folder-tree-end
   那一段載進 vm 跑。抄一份就等於沒測到真的程式 —— 兩邊會慢慢分岔，
   而分岔的症狀是「測試全過、畫面是壞的」。

   這一段刻意是純函式（不碰 DOM），所以不需要任何 stub。 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

let OK = 0, FAIL = 0;
function check(name, cond, extra) {
  if (cond) { OK++; console.log('  PASS  ' + name); }
  else { FAIL++; console.log('  FAIL  ' + name + '  → ' + JSON.stringify(extra)); }
}
function head(t) { console.log('\n' + t); }

const src = fs.readFileSync(path.join(__dirname, '..', 'app', 'static', 'admin.js'), 'utf8');
const m = src.match(/\/\/ @@folder-tree-begin([\s\S]*?)\/\/ @@folder-tree-end/);
if (!m) { console.log('  FAIL  admin.js 裡找不到 @@folder-tree-begin/end 標記'); process.exit(1); }
const sb = {};
vm.createContext(sb);
vm.runInContext(m[1] + `
  Object.assign(globalThis, { segsOf, leafOf, folderState, folderStats, buildFolderTree,
    folderContext, folderCountText, visibleFolderRows, hitRange, grantPayload, aclRuleSummary });`, sb);
const T = sb;

// 形狀照 /api/folders/acl 的 folders（acl.folder_counts）：依路徑排序、depth 從 1 起算
const F = (folder, extra = {}) => ({
  folder, depth: folder.split('/').filter(Boolean).length, videos: 0, photos: 0, documents: 0,
  restricted: false, covered_by: null, ...extra,
});
const folders = [
  F('/HBO', { videos: 40 }),
  F('/HBO/House of the Dragon', { videos: 4 }),
  F('/HBO/Westworld', { videos: 36, photos: 2 }),
  F('/HBO/Westworld/Season 01', { videos: 10 }),
  F('/HBO/Westworld/Season 02', { videos: 10 }),
  F('/HomeVideo', { videos: 3 }),
  F('/HomeVideo/2024', { videos: 3 }),
  F('/Movies', { videos: 5 }),
  F('/Movies/2024', { videos: 5 }),
  F('/Private', { restricted: true, documents: 2 }),
  F('/Private/Stuff', { covered_by: '/Private', documents: 2 }),
  // 深層的規則：父層（第 5 層）不在清單裡 —— folder_counts 的真實情況
  F('/Deep/a/b/c', {}),
  F('/Deep/a/b/c/d/e', { restricted: true }),
];
const names = rows => rows.map(r => r.node.folder);

head('[1] buildFolderTree');
const tree = T.buildFolderTree(folders);
check('最上層只有頂層資料夾（排序過）',
  JSON.stringify(tree.roots.map(n => n.name)) === JSON.stringify(['Deep/a/b/c', 'HBO', 'HomeVideo', 'Movies', 'Private']),
  tree.roots.map(n => n.name));
const ww = tree.byPath.get('/HBO/Westworld');
check('父子關係算好了', ww.parent === tree.byPath.get('/HBO') && ww.children.length === 2,
  { parent: ww.parent && ww.parent.folder, kids: ww.children.map(c => c.name) });
check('depth 是樹裡的層級（0 起算）', ww.depth === 1 && ww.children[0].depth === 2, ww.depth);
check('節點帶著狀態與數量',
  ww.state === 'free' && ww.videos === 36 && ww.photos === 2
  && tree.byPath.get('/Private').state === 'restricted'
  && tree.byPath.get('/Private/Stuff').state === 'covered'
  && tree.byPath.get('/Private/Stuff').covered_by === '/Private', ww.state);
const deep = tree.byPath.get('/Deep/a/b/c/d/e');
check('父層不在清單裡的深層規則，掛到最近的祖先（不是變成孤兒掉到最上層）',
  deep.parent === tree.byPath.get('/Deep/a/b/c') && !tree.roots.includes(deep),
  deep.parent && deep.parent.folder);
check('跳層的節點名稱帶著中間那幾段（d/e），不是只有 e',
  deep.name === 'd/e' && tree.byPath.get('/Deep/a/b/c').name === 'Deep/a/b/c', deep.name);
check('上層完全不在清單裡的會當作最上層（不會不見）',
  tree.roots.includes(tree.byPath.get('/Deep/a/b/c')), tree.roots.map(n => n.folder));
check('同名資料夾有標出來（2024 兩個）',
  tree.byPath.get('/Movies/2024').dupName && tree.byPath.get('/HomeVideo/2024').dupName
  && !ww.dupName, null);
check('空清單不會炸', T.buildFolderTree([]).roots.length === 0 && T.buildFolderTree(null).byPath.size === 0);

head('[2] 展開／收合');
const show = { restricted: true, covered: false };
let v = T.visibleFolderRows(tree, { expanded: new Set(), show });
check('初始只顯示最上層', names(v.rows).every(f => T.segsOf(f).length === 1 || f === '/Deep/a/b/c')
  && v.rows.length === 5, names(v.rows));
check('有子資料夾的列標了 hasKids、而且是收合的',
  v.rows.find(r => r.node.folder === '/HBO').hasKids && !v.rows.find(r => r.node.folder === '/HBO').open);
const exp = new Set(['/HBO']);
v = T.visibleFolderRows(tree, { expanded: exp, show });
check('展開 HBO → 子資料夾出現、孫子還沒有',
  names(v.rows).includes('/HBO/Westworld') && !names(v.rows).includes('/HBO/Westworld/Season 01'),
  names(v.rows));
exp.add('/HBO/Westworld');
v = T.visibleFolderRows(tree, { expanded: exp, show });
check('再展開 Westworld → Season 01/02 依序接在它後面',
  names(v.rows).join('|').includes('/HBO/Westworld|/HBO/Westworld/Season 01|/HBO/Westworld/Season 02'),
  names(v.rows));
exp.delete('/HBO');
v = T.visibleFolderRows(tree, { expanded: exp, show });
check('收合 HBO → 整棵子樹一起收起來（即使 Westworld 還記得是展開的）',
  !names(v.rows).some(f => f.startsWith('/HBO/')), names(v.rows));

head('[3] 狀態篩選');
v = T.visibleFolderRows(tree, { expanded: new Set(['/Private']), show: { restricted: true, covered: false } });
const priv = v.rows.find(r => r.node.folder === '/Private');
check('預設：已受限顯示、被涵蓋不顯示 → /Private 沒有可展開的子資料夾',
  priv && !priv.hasKids && !names(v.rows).includes('/Private/Stuff'), names(v.rows));
v = T.visibleFolderRows(tree, { expanded: new Set(['/Private']), show: { restricted: true, covered: true } });
check('打開「被涵蓋」→ /Private/Stuff 出現（不能選，但找得到）',
  names(v.rows).includes('/Private/Stuff'), names(v.rows));
v = T.visibleFolderRows(tree, { expanded: new Set(), show: { restricted: false, covered: false } });
check('關掉「已受限」→ /Private 不在最上層', !names(v.rows).includes('/Private'), names(v.rows));
const st = T.folderStats(folders);
check('可選／已受限／被涵蓋的計數', st.free === 10 && st.restricted === 2 && st.covered === 1, st);

head('[4] 搜尋');
const before = new Set(['/Movies']);
v = T.visibleFolderRows(tree, { expanded: before, query: 'season', show });
check('搜深層資料夾：命中的列出現', names(v.rows).includes('/HBO/Westworld/Season 01')
  && names(v.rows).includes('/HBO/Westworld/Season 02'), names(v.rows));
check('祖先自動留著而且展開（不會浮在半空）',
  v.rows.find(r => r.node.folder === '/HBO').open && v.rows.find(r => r.node.folder === '/HBO/Westworld').open,
  names(v.rows));
check('不相干的不出現', !names(v.rows).includes('/Movies'), names(v.rows));
check('命中與脈絡分得出來（hit 只標在命中的列）',
  v.rows.find(r => r.node.folder === '/HBO/Westworld/Season 01').hit
  && !v.rows.find(r => r.node.folder === '/HBO').hit, null);
check('搜尋不會改掉 expanded（清掉搜尋就回到原本的樣子）',
  before.size === 1 && before.has('/Movies'), [...before]);
v = T.visibleFolderRows(tree, { expanded: new Set(), query: 'west', show });
check('比對完整路徑：west 找得到 /HBO/Westworld', names(v.rows).includes('/HBO/Westworld'), names(v.rows));
v = T.visibleFolderRows(tree, { expanded: new Set(), query: 'HBO', show });
check('打上層名稱也找得到底下的東西（路徑裡有 HBO）',
  names(v.rows).includes('/HBO/Westworld/Season 01'), names(v.rows));
v = T.visibleFolderRows(tree, { expanded: new Set(), query: 'season', show, collapsed: new Set(['/HBO/Westworld']) });
check('搜尋中手動收合的節點照樣收合',
  names(v.rows).includes('/HBO/Westworld') && !names(v.rows).includes('/HBO/Westworld/Season 01'), names(v.rows));
v = T.visibleFolderRows(tree, { expanded: new Set(), query: 'stuff', show: { restricted: true, covered: false } });
check('命中但被篩選藏起來的 → 列不出現，但回報 hiddenHits（找不到 ≠ 被藏起來）',
  v.rows.length === 0 && v.hiddenHits === 1, { rows: names(v.rows), hidden: v.hiddenHits });
v = T.visibleFolderRows(tree, { expanded: new Set(), query: 'stuff', show: { restricted: false, covered: true } });
check('被涵蓋的命中：祖先（已受限）當作脈絡照樣出現',
  JSON.stringify(names(v.rows)) === JSON.stringify(['/Private', '/Private/Stuff']), names(v.rows));
check('標亮：名稱裡有才標', JSON.stringify(T.hitRange('Westworld', 'west')) === '[0,4]'
  && T.hitRange('Season 01', 'hbo') === null && T.hitRange('x', '') === null);

head('[5] 顯示文字');
check('上層脈絡取最後兩層', T.folderContext('/Movies/2024') === 'Movies / 2024'
  && T.folderContext('/a/b/c') === 'b / c' && T.folderContext('/x') === 'x');
const ct = T.folderCountText({ videos: 36, photos: 2, documents: 0 });
check('數量只寫有值的', ct.full === '影片 36 · 相片 2' && ct.short === '38 個項目', ct);
check('全部是 0 就不寫', T.folderCountText({}).full === '' && T.folderCountText({}).short === '');

head('[6] 授權名單（安全不變量）');
const users = [
  { id: 1, name: 'Mark', is_admin: false }, { id: 2, name: 'Alice', is_admin: false },
  { id: 3, name: 'Bob', is_admin: false }, { id: 9, name: 'John', is_admin: true },
  { id: 10, name: 'Admin2', is_admin: true },
];
const rule = { id: 5, prefix: '/Private', user_ids: [1, 9], user_names: ['Mark', 'John'] };
check('先有授權、後來升管理員的 id 會被帶回去（別人按儲存不會安靜地收回）',
  JSON.stringify(T.grantPayload(rule, users, [1, 2]).sort()) === JSON.stringify([1, 2, 9]),
  T.grantPayload(rule, users, [1, 2]));
check('全部取消勾選時，那筆管理員的授權仍然保留',
  JSON.stringify(T.grantPayload(rule, users, [])) === '[9]', T.grantPayload(rule, users, []));
check('沒有授權的管理員不會被硬塞進去（角色歸角色、授權歸授權）',
  !T.grantPayload(rule, users, [1]).includes(10), T.grantPayload(rule, users, [1]));
check('沒有重複 id', T.grantPayload(rule, users, [1, 1]).length === 2);

const sum = T.aclRuleSummary(rule, users, folders);
check('「N 人可看」只算一般帳號（管理員靠角色，不算在裡面）', sum.viewers === 1 && sum.preview === 'Mark', sum);
check('管理員另外計數、保有授權的管理員另外列出',
  sum.admins === 2 && JSON.stringify(sum.keptAdmins) === '["John"]', sum);
check('涵蓋的子資料夾用相對路徑', JSON.stringify(sum.kids) === '["Stuff"]', sum.kids);
const many = T.aclRuleSummary({ ...rule, user_ids: [1, 2, 3] }, users, []);
check('三個以上：Mark、Alice +1', many.preview === 'Mark、Alice +1' && many.viewers === 3, many);
const none = T.aclRuleSummary({ ...rule, user_ids: [9] }, users, []);
check('只有管理員有授權 → 0 人可看（要顯示警告的狀態）', none.viewers === 0, none);

console.log(`\n${'='.repeat(52)}\n通過 ${OK}，失敗 ${FAIL}`);
process.exit(FAIL ? 1 : 0);
