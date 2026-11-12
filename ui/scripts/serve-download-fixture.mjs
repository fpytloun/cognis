import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const root = resolve(process.argv[2]);
const port = Number(process.argv[3]);
const binary = Buffer.from([0, 1, 127, 128, 255]);
const files = {
  '/download.pdf': ['report.pdf', 'application/pdf', Buffer.from('%PDF-synthetic-download\n')],
  '/download.html': ['report.html', 'text/html', Buffer.from('<h1>Downloaded, never executed</h1>')],
  '/download.md': ['report.md', 'text/markdown', Buffer.from('# Downloaded report\n')],
  '/binary.bin': ['binary.bin', 'application/octet-stream', binary],
};
createServer(async (req, res) => {
  const path = new URL(req.url, 'http://127.0.0.1').pathname;
  if (path === '/slow.bin') {
    const rest = Buffer.alloc(1024 * 1024);
    res.writeHead(200, {
      'content-type': 'application/octet-stream', 'content-length': rest.length + binary.length,
      'content-disposition': 'attachment; filename="slow.bin"',
    });
    res.write(binary);
    const timer = setTimeout(() => res.end(rest), 5000);
    res.on('close', () => clearTimeout(timer));
    return;
  }
  if (path === '/cookie.bin') {
    if (!req.headers.cookie?.includes('cognis_session=synthetic-download-cookie')) {
      res.writeHead(403); res.end(); return;
    }
    res.writeHead(200, { 'content-type': 'application/octet-stream', 'content-disposition': 'attachment; filename="cookie.bin"' });
    res.end(binary); return;
  }
  if (path === '/redirect') {
    res.writeHead(302, { location: '/binary.bin' }); res.end(); return;
  }
  if (path === '/rejected-redirect') {
    res.writeHead(302, { location: 'file:///etc/passwd' }); res.end(); return;
  }
  if (path === '/interrupted.bin') {
    res.writeHead(200, {
      'content-type': 'application/octet-stream', 'content-length': 1024 * 1024,
      'content-disposition': 'attachment; filename="interrupted.bin"',
    });
    res.write(binary);
    const timer = setTimeout(() => res.destroy(), 200);
    res.on('close', () => clearTimeout(timer));
    return;
  }
  if (path === '/denied') {
    res.writeHead(403, { 'content-type': 'application/json' }); res.end('{"detail":"forbidden"}'); return;
  }
  const file = files[path];
  if (file) {
    const [name, mime, bytes] = file;
    res.writeHead(200, {
      'content-type': mime, 'content-length': bytes.length,
      'content-disposition': `attachment; filename="${name}"`,
    });
    res.end(bytes); return;
  }
  if (path !== '/') { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { 'content-type': 'text/html' });
  res.end(await readFile(resolve(root, 'index.html')));
}).listen(port, '127.0.0.1', () => console.log(`Download fixture listening on 127.0.0.1:${port}`));
