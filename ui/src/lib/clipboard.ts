function isPdfLike(type: string, name: string): boolean {
  return type.toLowerCase() === 'application/pdf' || name.toLowerCase().endsWith('.pdf');
}

function fallbackFilename(type: string): string {
  const suffix = type.toLowerCase() === 'application/pdf' ? 'pdf' : 'bin';
  return `pasted-document-${Date.now()}.${suffix}`;
}

function ensureNamedFile(file: File | Blob, filename?: string): File {
  const currentName = file instanceof File ? file.name : '';
  const name = currentName || filename || fallbackFilename(file.type);
  if (file instanceof File && file.name) return file;
  return new File([file], name, { type: file.type || 'application/octet-stream' });
}

function fileKey(file: File): string {
  return `${file.name}:${file.type}:${file.size}:${file.lastModified}`;
}

function addUnique(files: File[], seen: Set<string>, file: File): void {
  const key = fileKey(file);
  if (seen.has(key)) return;
  seen.add(key);
  files.push(file);
}

export async function pastedFilesFromClipboardEvent(
  event: ClipboardEvent,
  navigatorLike: Pick<Navigator, 'clipboard'> | null = typeof navigator === 'undefined' ? null : navigator
): Promise<File[]> {
  const files = pastedFilesFromClipboardData(event.clipboardData);
  if (files.length > 0) return files;
  return readPastedFilesFromNavigator(navigatorLike);
}

export function pastedFilesFromClipboardData(data: DataTransfer | null): File[] {
  const files: File[] = [];
  const seen = new Set<string>();

  if (data?.files) {
    for (const file of Array.from(data.files)) {
      addUnique(files, seen, ensureNamedFile(file));
    }
  }

  if (data?.items) {
    for (const item of Array.from(data.items)) {
      if (item.kind !== 'file') continue;
      const file = item.getAsFile();
      if (file) addUnique(files, seen, ensureNamedFile(file));
    }
  }

  return files;
}

export async function readPastedFilesFromNavigator(
  navigatorLike: Pick<Navigator, 'clipboard'> | null = typeof navigator === 'undefined' ? null : navigator
): Promise<File[]> {
  const files: File[] = [];
  const seen = new Set<string>();
  const clipboard = navigatorLike?.clipboard;
  if (typeof clipboard?.read !== 'function') return [];

  try {
    for (const item of await clipboard.read()) {
      const type = item.types.find((candidate) => candidate === 'application/pdf');
      if (!type) continue;
      const blob = await item.getType(type);
      addUnique(files, seen, ensureNamedFile(blob, fallbackFilename(type)));
    }
  } catch {
    return [];
  }

  return files.filter((file) => isPdfLike(file.type, file.name));
}
