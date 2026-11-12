import { mount } from 'svelte';
import DownloadMenu from '../src/lib/components/rich/DownloadMenu.svelte';
import AttachmentSaveButton from '../src/lib/components/AttachmentSaveButton.svelte';
import { downloadBlob, downloadUrl } from '../src/lib/attachments/save';

const exports = {
  workflow: ['workflow.yaml', 'name: Download acceptance\n'],
  recovery: ['cognis-recovery-codes.txt', 'synthetic-recovery-code\n'],
  skill: ['SKILL.md', '# Download acceptance\n'],
  package: ['skill.zip', new Uint8Array([0, 1, 127, 128, 255])],
  recording: ['ws-recording.jsonl', '{"type":"synthetic-frame"}\n'],
} as const;

mount(DownloadMenu, {
  target: document.getElementById('formats')!,
  props: { pdfUrl: '/download.pdf', htmlUrl: '/download.html', markdownUrl: '/download.md' },
});
mount(AttachmentSaveButton, {
  target: document.getElementById('attachment')!,
  props: { filename: 'attachment.bin', resolveUrl: async () => '/binary.bin' },
});

const fixture = {
  saveBlob(kind: keyof typeof exports) {
    const [name, bytes] = exports[kind];
    downloadBlob(new Blob([bytes]), name);
  },
  saveUrl: downloadUrl,
};
Object.assign(window, { downloadFixture: fixture });
for (const kind of Object.keys(exports) as (keyof typeof exports)[]) {
  const button = document.createElement('button');
  button.textContent = `Export ${kind}`;
  button.onclick = () => fixture.saveBlob(kind);
  document.body.append(button);
}
