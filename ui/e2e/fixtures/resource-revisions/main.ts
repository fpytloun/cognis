import { mount } from 'svelte';
import { auth } from '../../../src/lib/stores/auth';
import Fixture from './Fixture.svelte';
await auth.bootstrap();
mount(Fixture, { target: document.getElementById('fixture')! });
