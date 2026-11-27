// 求职助手（hiremate）· Antigravity 原生侧边栏与分屏看板 Sidecar
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { SidecarApp } from 'sidecar_sdk';

const app = new SidecarApp();

const ALLOWED_UI_PATCH_KEYS = new Set([
  'mode_discover',
  'mode_reply',
  'icebreaker_mode',
  'target_roles',
  'preferred_cities',
  'remote_bigtech_flex',
  'min_salary_k',
  'salary_match_mode',
  'outsourcing_policy',
  'onsite_policy',
  'headhunter_policy',
  'inbound_resume_policy',
  'past_employers',
  'blocked_companies',
  'exclude_keywords',
  'schedule',
]);

function resolvePaths() {
  const homeDir = process.env.HIREMATE_HOME || join(homedir(), '.hiremate');
  const wsDir = process.env.HIREMATE_WORKSPACE || join(homeDir, 'default');
  const candidates = [
    ...(process.env.HIREMATE_CLI_PATH ? [process.env.HIREMATE_CLI_PATH] : []),
    join(homeDir, 'skill', 'hiremate', 'hiremate.py'),
    join(homedir(), '.gemini', 'config', 'plugins', 'hiremate', 'skills', 'hiremate', 'hiremate.py'),
    join(homedir(), '.gemini', 'config', 'skills', 'hiremate', 'hiremate.py'),
  ];
  const cliPath = candidates.find((p) => existsSync(p)) || candidates[0];
  const dashPath = join(wsDir, 'dashboard.html');
  return { homeDir, wsDir, cliPath, dashPath };
}

function sanitizeUiPatch(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
  const out = {};
  for (const [k, v] of Object.entries(raw)) {
    if (!ALLOWED_UI_PATCH_KEYS.has(k)) continue;
    if (k === 'schedule') {
      if (v && typeof v === 'object' && !Array.isArray(v)) {
        const sc = {};
        for (const sk of ['weekdays_only', 'max_per_round', 'max_per_day', 'min_interval_minutes', 'active_windows']) {
          if (sk in v) sc[sk] = v[sk];
        }
        if (Object.keys(sc).length > 0) out.schedule = sc;
      }
    } else {
      out[k] = v;
    }
  }
  return out;
}

function injectSidecarPreload(rawHtml) {
  const preloadTag = '<script src="/preload.js"></script>';
  if (rawHtml.includes('/preload.js')) return rawHtml;
  const headMatch = rawHtml.match(/<head[^>]*>/i);
  if (headMatch) {
    const idx = headMatch.index + headMatch[0].length;
    return rawHtml.slice(0, idx) + '\n  ' + preloadTag + rawHtml.slice(idx);
  }
  return preloadTag + rawHtml;
}

function runCliJson(args) {
  const { homeDir, wsDir, cliPath } = resolvePaths();
  if (!existsSync(cliPath)) {
    return { ok: false, error: '未找到本机求职助手 CLI 入口（~/.hiremate/skill/hiremate/hiremate.py）' };
  }
  const pySpecs = [
    ...(process.env.HIREMATE_PYTHON ? [[process.env.HIREMATE_PYTHON, []]] : []),
    ['python3', []],
    ...(process.platform === 'win32' ? [['py', ['-3']], ['python', []]] : [['python', []]]),
  ];
  let lastErr = null;
  for (const [bin, prefixArgs] of pySpecs) {
    try {
      const stdout = execFileSync(bin, [...prefixArgs, cliPath, '--home', homeDir, '--workspace', wsDir, ...args], {
        timeout: 15_000,
        encoding: 'utf-8',
        env: { ...process.env, HIREMATE_DISABLE_UPDATE_CHECK: '1', PYTHONUTF8: '1' },
      });
      return JSON.parse(stdout);
    } catch (err) {
      lastErr = err;
      if (err && (err.code === 'ENOENT' || /Microsoft Store/i.test(String(err.stderr || err.message || '')))) {
        continue;
      }
      if (err && err.stdout) {
        try {
          return JSON.parse(String(err.stdout));
        } catch {
          // fall through
        }
      }
      break;
    }
  }
  return { ok: false, error: String((lastErr && lastErr.message) || '调用求职助手 CLI 失败') };
}

function renderBoardHtml() {
  const { cliPath, dashPath } = resolvePaths();
  if (existsSync(cliPath)) {
    runCliJson(['build-dashboard']);
  }
  if (existsSync(dashPath)) {
    return injectSidecarPreload(readFileSync(dashPath, 'utf-8'));
  }
  return injectSidecarPreload(
    '<!doctype html><meta charset="utf-8"><head></head><body style="font:14px system-ui;padding:28px;color:#334155"><h3>求职助手尚未初始化</h3><p>请在聊天框输入「帮我初始化求职助手」完成 5 分钟建档后自动生成可视化求职看板。</p></body>'
  );
}

app.page('/', () => renderBoardHtml());
app.page('/aux', () => renderBoardHtml());

app.api('/api/config/save', (data) => {
  const patch = sanitizeUiPatch(data && data.patch);
  const ackAutoRisk = Boolean(data && data.ack_auto_risk);
  const cliArgs = ['config', 'set', '--patch-json', JSON.stringify(patch)];
  if (ackAutoRisk) cliArgs.push('--ack-auto-risk');
  return runCliJson(cliArgs);
});

app.run();
