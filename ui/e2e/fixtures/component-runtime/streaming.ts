import { mount } from 'svelte';
import '../../../src/app.css';
import StreamingFixture from '../../../src/routes/streaming-fixture/+page.svelte';

mount(StreamingFixture, { target: document.getElementById('app')! });
