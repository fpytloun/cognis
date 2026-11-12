import { mount } from 'svelte';
import RichGridLayoutHarness from '../../src/lib/components/feedback/annotations/RichGridLayoutHarness.svelte';
import '../../src/standalone.css';

const mode = new URL(location.href).searchParams.get('mode');
mount(RichGridLayoutHarness, { target: document.getElementById('fixture')!,
  props: { mode: mode === 'comment' || mode === 'view' ? mode : 'unbound' } });
