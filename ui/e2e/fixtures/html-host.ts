import { mount } from 'svelte';
import { auth } from '../../src/lib/stores/auth';
import HtmlHostBrowser from './HtmlHostBrowser.svelte';
import '../../src/app.css';

await auth.login('html-host@cognis.test', 'synthetic-browser-fixture');
mount(HtmlHostBrowser, { target: document.getElementById('fixture')! });
