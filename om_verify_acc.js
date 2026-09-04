const { chromium } = require('playwright-core');
(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.LOCALAPPDATA + '/ms-playwright/chromium-1234/chrome-win64/chrome.exe',
    headless: true
  });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  const lj = await (await fetch('http://212.129.154.41:8000/api/auth/login', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({email:'demo123@openmontage.cn', password:'demo1234'})
  })).json();
  const errors = [];
  const pg = await ctx.newPage();
  pg.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/404|favicon/.test(m.text())) errors.push('CONSOLE: ' + m.text()); });
  await pg.addInitScript(({token}) => {
    localStorage.setItem('om_token', token);
    localStorage.setItem('om_page', 'account');
  }, {token: lj.token});
  await pg.goto('http://212.129.154.41:8000/prototype', { waitUntil: 'networkidle', timeout: 45000 });
  await pg.waitForSelector('#accBalance', { timeout: 15000 });
  await pg.waitForTimeout(2500);
  const info = await pg.evaluate(() => ({
    navActive: document.querySelector('.nav-item.active')?.getAttribute('data-page'),
    navItems: [...document.querySelectorAll('.nav-item')].map(n => n.textContent.trim()),
    crumb: document.getElementById('crumb')?.textContent,
    balance: document.getElementById('accBalance')?.textContent,
    balanceUsd: document.getElementById('accBalanceUsd')?.textContent,
    spent: document.getElementById('accSpent')?.textContent,
    byok: document.getElementById('accByok')?.textContent,
    jobs: document.getElementById('accJobs')?.textContent,
    note: (document.getElementById('accNote')?.textContent||'').slice(0,80),
    costsMsg: document.getElementById('accCosts')?.textContent?.slice(0,80),
    side: document.getElementById('sideBalance')?.textContent,
    top: document.getElementById('topBalance')?.textContent,
  }));
  console.log('ACC', JSON.stringify(info, null, 1));
  // 充值弹层
  await pg.click('.page-head .btn-primary');   // ＋充值
  await pg.waitForTimeout(400);
  const modal = await pg.evaluate(() => ({
    open: document.getElementById('recharMask').classList.contains('show'),
    val: document.getElementById('recharInput')?.value,
  }));
  console.log('RECHARGE_MODAL', JSON.stringify(modal));
  await pg.evaluate(() => document.getElementById('recharMask').classList.remove('show'));
  await pg.screenshot({ path: 'D:/github/OpenMontage-dev/账户中心验证.png', fullPage: true });
  console.log('ERRORS:', errors.length ? errors.join('\n') : 'none');
  await browser.close();
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
