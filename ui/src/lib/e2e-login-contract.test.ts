import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const source = fs.readFileSync(path.resolve('e2e/helpers.ts'), 'utf8');

describe('shared E2E login helper contract', () => {
  it('uses the login response and authenticated UI instead of current-path navigation', () => {
    expect(source).toContain("new URL(response.url()).pathname === '/api/auth/login'");
    expect(source).toContain("response.request().method() === 'POST'");
    expect(source).toContain('if (!loginResponse.ok())');
    expect(source).toContain("page.request.get('/api/auth/me')");
    expect(source).toContain('if (sessionResponse.ok())');
    expect(source).toContain('await expect(controlCenter).toBeVisible()');
    expect(source).toContain("toHaveCount(0)");
    expect(source).not.toContain('page.waitForURL');
  });

  it('redacts the configured password from bounded failure diagnostics', () => {
    expect(source).toContain(".split(ADMIN_PASSWORD).join('[redacted]')");
    expect(source).toContain('.slice(0, 500)');
  });
});
