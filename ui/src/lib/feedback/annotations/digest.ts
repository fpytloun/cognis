export async function textDigest(text: string): Promise<string> {
  return bytesDigest(new TextEncoder().encode(text));
}
export async function bytesDigest(bytes: BufferSource): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, '0')).join('');
}
