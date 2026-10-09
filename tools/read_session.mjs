/**
 * 读取 DSH 的历史会话，导出成可读文本。
 *
 * 背景（为什么需要这么个东西）
 * ---------------------------
 * DSH 把每个会话持久化到：
 *     ~/.dsh/sessions/--<工作目录编码>--/<会话ID>/session.v3.jsonl.zstd
 *
 * 这个 .zstd 文件**不是单个 zstd 流，而是多个独立帧拼接**的
 * （每次追加写一帧）。所以：
 *   - zstdDecompressSync(buf)  只解第一帧 → 只能拿到会话头（0.3 KB）
 *   - createZstdDecompress() 流式解压也停在第一帧
 * 正确做法是**按魔数 28 B5 2F FD 切帧，逐帧解压再拼接**。
 * 实测某会话：192 KB 文件 → 81 帧 → 674 KB → 166 行事件。
 *
 * DSH 代码里也有对应实现（dsh-session-persistence-jsonl 的
 * NodePrivateZstdFrameDecoder，注释写着 "multi-frame decoder"），
 * 这里是自己按同样思路复现，不依赖 DSH 内部 API。
 *
 * 用法（Electron 当 Node 跑，因为 Electron 的 node:zlib 带 zstd）：
 *     set ELECTRON_RUN_AS_NODE=1
 *     "D:\DSH\DSH Desktop\DSH Desktop.exe" tools\read_session.mjs list
 *     "D:\DSH\DSH Desktop\DSH Desktop.exe" tools\read_session.mjs dump <会话ID> [maxChars]
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import zlib from 'node:zlib';

const ZSTD_MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]);
const SESSIONS_ROOT = path.join(os.homedir(), '.dsh', 'sessions');

/** 把会话文件按帧切开、逐帧解压，返回 UTF-8 文本。 */
function decompressSessionFile(file) {
  const buf = fs.readFileSync(file);
  const starts = [];
  let i = 0;
  while ((i = buf.indexOf(ZSTD_MAGIC, i)) !== -1) {
    starts.push(i);
    i += 1;
  }
  if (starts.length === 0) return '';

  const parts = [];
  for (let k = 0; k < starts.length; k++) {
    const start = starts[k];
    const end = k + 1 < starts.length ? starts[k + 1] : buf.length;
    try {
      parts.push(zlib.zstdDecompressSync(buf.subarray(start, end)));
    } catch {
      // 单帧坏了不该让整个会话读不出来
    }
  }
  return Buffer.concat(parts).toString('utf8');
}

/** 列出所有工作区下的会话。 */
function listSessions() {
  const out = [];
  if (!fs.existsSync(SESSIONS_ROOT)) return out;

  for (const ws of fs.readdirSync(SESSIONS_ROOT)) {
    const wsDir = path.join(SESSIONS_ROOT, ws);
    if (!fs.statSync(wsDir).isDirectory()) continue;
    for (const sid of fs.readdirSync(wsDir)) {
      const f = path.join(wsDir, sid, 'session.v3.jsonl.zstd');
      if (!fs.existsSync(f)) continue;
      const st = fs.statSync(f);
      let title = '';
      let cwd = '';
      try {
        const text = decompressSessionFile(f);
        const lines = text.split('\n').filter(Boolean);
        for (const l of lines) {
          const o = JSON.parse(l);
          if (o.type === 'session') cwd = o.cwd || '';
          if (o.type === 'session/title') title = (o.data && o.data.title) || '';
        }
      } catch { /* 忽略读不出来的 */ }
      out.push({
        workspace: ws,
        id: sid,
        bytes: st.size,
        mtime: st.mtime.toISOString().slice(0, 19).replace('T', ' '),
        title,
        cwd,
      });
    }
  }
  out.sort((a, b) => (a.mtime < b.mtime ? 1 : -1));
  return out;
}

function findSessionFile(idFragment) {
  for (const s of listSessions()) {
    if (s.id.includes(idFragment)) {
      return path.join(SESSIONS_ROOT, s.workspace, s.id, 'session.v3.jsonl.zstd');
    }
  }
  return null;
}

/**
 * 从事件里挖出可读文本。DSH 的正文可能藏在 data / content 数组里。
 *
 * 助手消息的 content 是「parts 数组」，里面既有 {"type":"text"} 也有
 * {"type":"tool-call"}。tool-call 由独立的 tool/call 事件另画一行，
 * 所以这里只取 text 部分，避免把整坨 JSON 打出来。
 */
function pickText(o, { textOnly = false } = {}) {
  const cand = o.data ?? o;
  let c = cand.content ?? cand.text ?? cand.message ?? cand.body ?? '';

  if (Array.isArray(c)) {
    const parts = [];
    for (const x of c) {
      if (typeof x === 'string') { parts.push(x); continue; }
      if (!x || typeof x !== 'object') continue;
      if (x.type === 'tool-call' || x.type === 'tool_call') continue;
      if (x.type && x.type !== 'text' && x.type !== 'input_text') {
        if (textOnly) continue;
      }
      const s = x.text ?? x.content ?? '';
      if (typeof s === 'string' && s.trim()) parts.push(s);
    }
    c = parts.join('\n');
  } else if (c && typeof c === 'object') {
    c = c.text ?? c.content ?? (textOnly ? '' : JSON.stringify(c));
  }
  if (c == null) c = '';
  return typeof c === 'string' ? c : JSON.stringify(c);
}

/** 把事件流渲染成可读文本。 */
function render(text, maxChars = 600) {
  const lines = text.split('\n').filter(Boolean);
  const out = [];
  let n = 0;

  const clip = (s) => {
    s = String(s ?? '').replace(/\s+/g, ' ').trim();
    return s.length > maxChars ? s.slice(0, maxChars) + ` …[+${s.length - maxChars}字]` : s;
  };

  for (const l of lines) {
    let o;
    try { o = JSON.parse(l); } catch { continue; }
    const t = o.type;

    if (t === 'session') {
      out.push(`### 会话 ${o.id}`);
      out.push(`  创建时间: ${o.createdAt || '?'}`);
      out.push(`  工作目录: ${o.cwd || '?'}`);
      out.push('');
    } else if (t === 'session/title') {
      out.push(`### 标题: ${(o.data && o.data.title) || ''}`);
      out.push('');
    } else if (t === 'user/message') {
      out.push(`\n【用户】${clip(pickText(o))}`);
      n++;
    } else if (t === 'assistant/message') {
      out.push(`\n【助手】${clip(pickText(o))}`);
      n++;
    } else if (t === 'tool/call') {
      const d = o.data || o;
      const name = d.name || d.toolName || d.tool || '?';
      const args = JSON.stringify(d.arguments ?? d.input ?? d.args ?? {});
      out.push(`  · 调用 ${name}(${clip(args, 200)})`);
    } else if (t === 'tool/result') {
      const d = o.data || o;
      const c = d.content ?? d.result ?? d.output ?? d.text ?? '';
      const s = typeof c === 'string' ? c : JSON.stringify(c);
      if (s && s !== '""' && s !== '{}') out.push(`    → ${clip(s, 200)}`);
    }
  }
  return { text: out.join('\n'), count: n };
}

function main() {
  const [cmd, arg, extra] = process.argv.slice(2);

  if (cmd === 'list' || !cmd) {
    const all = listSessions();
    console.log(`共 ${all.length} 个历史会话：\n`);
    console.log('  修改时间             大小      会话ID                                        标题');
    console.log('  ' + '-'.repeat(100));
    for (const s of all) {
      const mb = (s.bytes / 1048576).toFixed(2);
      console.log(`  ${s.mtime}  ${mb.padStart(6)} MB  ${s.id.slice(0, 44).padEnd(46)} ${s.title || ''}`);
    }
    console.log('\n用 dump <会话ID片段> 查看内容。');
    return 0;
  }

  if (cmd === 'dump') {
    if (!arg) { console.error('用法: dump <会话ID片段> [每条最大字数]'); return 2; }
    const f = findSessionFile(arg);
    if (!f) { console.error(`没找到会话: ${arg}`); return 2; }
    const maxChars = extra ? parseInt(extra, 10) : 600;
    const text = decompressSessionFile(f);
    const { text: rendered, count } = render(text, maxChars);
    console.log(rendered);
    console.error(`\n[共 ${count} 条消息]`);
    return 0;
  }

  console.error('用法: read_session.mjs [list | dump <会话ID片段> [maxChars]]');
  return 2;
}

process.exit(main());
