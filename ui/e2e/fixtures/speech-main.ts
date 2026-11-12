import { mount } from 'svelte';
import '../../src/app.css';
import SpeechFixture from './SpeechFixture.svelte';

mount(SpeechFixture, { target: document.getElementById('fixture')! });
