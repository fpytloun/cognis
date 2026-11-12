import { mount } from 'svelte';
import Fixture from '../../../src/lib/components/feedback/annotations/TableRangeBrowserFixture.svelte';
import '../../../src/app.css';
import { auth } from '../../../src/lib/stores/auth';

const showReply = new URLSearchParams(location.search).has('reply');
if (showReply) await auth.bootstrap();
mount(Fixture, { target: document.getElementById('app')!, props: { showReply } });
