#!/usr/bin/env node
/* 工艺归并 UI 端到端验证：预览 → 发起 → 追踪样本去向（Playwright + 本地无头 Chromium）。
   前提：backend 19204、worker 运行中、frontend 静态服务 18204（serve-frontend.js）。 */
const { chromium } = require('playwright-core');
const fs = require('fs');

const WEB = 'http://127.0.0.1:18204';
const API = 'http://127.0.0.1:19204';
const SHOTS = '/workspace/test-artifacts/gb-3105';
const results = [];
const check = (name, ok, extra = '') => { results.push([name, !!ok]); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : '  ' + extra}`); };

async function api(token, method, path, body) {
  const r = await fetch(API + path, { method, headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: body ? JSON.stringify(body) : undefined });
  return { status: r.status, body: await r.json().catch(() => ({})) };
}
async function login(email) { const r = await api(null, 'POST', '/auth/login', { email, password: 'Demo123!' }); return r.body.access_token; }
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const analyst = await login('analyst@demo.local');
  // 预置数据：重复建档工艺 + 样本（UI 流程只负责预览/发起/追踪）
  const dup = (await api(analyst, 'POST', '/processes', { code: 'UI-DUP', name: '稳温基线界面重复档', temperature: 168, dwell: 0.8, pressure: 0.32, note: 'UI 验证' })).body;
  for (let i = 1; i <= 6; i++) await api(analyst, 'POST', '/samples', { code: `UID-${String(i).padStart(3, '0')}`, process_id: dup.id, lot: 'UI-01', force: 22 + i * 0.2 });

  const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));

  // 登录
  await page.goto(WEB);
  await page.fill('#login-form input[name=email]', 'analyst@demo.local');
  await page.fill('#login-form input[name=password]', 'Demo123!');
  await page.click('#login-form button');
  await page.waitForSelector('#merge-form', { timeout: 10000 });
  check('U1 登录后方案库显示归并区', await page.isVisible('.merge-section'));

  // 预览归并
  await page.selectOption('#merge-source', String(dup.id));
  const targetValue = await page.locator('#merge-target option').first().getAttribute('value');
  await page.selectOption('#merge-target', targetValue);
  await page.click('#merge-form button');
  await page.waitForSelector('.merge-preview', { timeout: 10000 });
  const previewText = await page.textContent('.merge-preview');
  check('U2 预览展示参数差异（温度不一致）', previewText.includes('不一致') && previewText.includes('168'));
  check('U3 预览展示样本影响（源6/合计）', previewText.includes('归并后合计'));
  await page.screenshot({ path: `${SHOTS}/ui-merge-preview.png`, fullPage: true });

  // 确认归并
  await page.click('#merge-confirm-form button');
  await page.waitForSelector('.merge-row', { timeout: 10000 });
  const flash = await page.textContent('#flash');
  check('U4 发起后提示别名生效', flash.includes('别名'));
  check('U5 归并列表出现迁移记录', (await page.textContent('.merge-list')).includes('UI-DUP'));
  await page.screenshot({ path: `${SHOTS}/ui-merge-migrating.png`, fullPage: true });

  // 页面轮询追踪到完成
  await page.waitForFunction(() => document.querySelector('.merge-row .badge')?.textContent.includes('归并完成'), null, { timeout: 60000 });
  const rowText = await page.textContent('.merge-row');
  check('U6 页面追踪到迁完且数量核对一致', /已迁 6 \/ 6/.test(rowText) && rowText.includes('剩余 0'));
  const aliasBadge = await page.textContent(`.process-row:has-text("UI-DUP")`);
  check('U7 旧编号在方案库中标记为别名', aliasBadge.includes('别名'));
  await page.screenshot({ path: `${SHOTS}/ui-merge-completed.png`, fullPage: true });

  // 收样页面：别名选项与自动归入
  await page.click('a[href="#samples"]');
  await page.waitForSelector('#sample-form');
  const aliasLabel = await page.locator('#sample-form select[name=process_id] option', { hasText: '别名 →' }).first().textContent();
  check('U8 收样下拉含别名选项并注明自动归入', aliasLabel.includes('UI-DUP') && aliasLabel.includes('别名'));
  await page.fill('#sample-form input[name=code]', 'UID-NEW-1');
  await page.selectOption('#sample-form select[name=process_id]', String(dup.id));
  await page.fill('#sample-form input[name=force]', '23.6');
  await page.click('#sample-form button.primary');
  await page.waitForFunction(() => !document.querySelector('#flash').hidden, null, { timeout: 10000 });
  const landed = (await api(analyst, 'GET', '/samples')).body.find(s => s.code === 'UID-NEW-1');
  check('U9 经别名收样自动归入目标工艺', landed && landed.process_id === +targetValue);
  await page.screenshot({ path: `${SHOTS}/ui-samples-alias.png`, fullPage: true });

  // 分析页面：别名不进复选框，目标标注含别名
  await page.click('a[href="#analysis"]');
  await page.waitForSelector('#assessment-form');
  const boxes = await page.$$eval('#assessment-form [name=process_ids]', els => els.map(e => e.value));
  check('U10 分析复选仅列正主工艺', !boxes.includes(String(dup.id)));
  const analysisText = await page.textContent('#assessment-form');
  check('U11 目标工艺标注含别名合并分析', analysisText.includes('含别名 UI-DUP'));
  await page.screenshot({ path: `${SHOTS}/ui-analysis-alias.png`, fullPage: true });

  // 只读账号
  await page.click('#logout');
  await page.fill('#login-form input[name=email]', 'viewer@demo.local');
  await page.fill('#login-form input[name=password]', 'Demo123!');
  await page.click('#login-form button');
  await page.waitForSelector('.topbar', { timeout: 10000 });
  await page.click('a[href="#processes"]');
  await page.waitForSelector('.merge-section', { timeout: 10000 });
  check('U12 只读账号可见归并进度但无发起表单', !(await page.$('#merge-form')) && (await page.textContent('.merge-section')).includes('归并完成'));

  check('U13 全程无前端脚本错误', errors.length === 0, errors.join(' | '));
  await browser.close();

  const failed = results.filter(r => !r[1]);
  console.log(`\n===== UI ${results.length - failed.length}/${results.length} 通过 =====`);
  process.exit(failed.length ? 1 : 0);
})().catch(e => { console.error('UI TEST ERROR:', e); process.exit(1); });
