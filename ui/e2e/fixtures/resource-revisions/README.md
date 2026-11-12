# Resource revision controls

This fixture mounts the real Library, chat document, attachment, and image
viewers. Playwright mocks only their API responses and checks light/dark themes,
desktop/phone widths, revision selection, and Escape behavior.

From `ui/`, serve it with:

```sh
npx vite dev --config e2e/fixtures/resource-revisions/vite.config.ts --host 127.0.0.1 --port 4198 --strictPort
```

In another terminal, run through the repository validation lock:

```sh
make -C .. validate COMMAND="bash -c 'cd ui && COGNIS_E2E_URL=http://127.0.0.1:4198 npx playwright test e2e/resource-revision-controls.spec.ts --workers=1 --retries=0'"
```

The test-only Vite plugin serves this one HTML entry before SvelteKit routing.
It does not add an application route or change production CSP.
The component-only entry supplies an empty public environment module because it
does not run SvelteKit's application bootstrap; API requests use their normal
same-origin defaults and are mocked by Playwright.
The normal auth bootstrap verifies the mocked `/api/auth/me` identity before
mounting viewers; document coordination and request ownership remain unchanged.
