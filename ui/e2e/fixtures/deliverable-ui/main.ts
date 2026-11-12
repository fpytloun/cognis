import { mount } from 'svelte';
import Browser from './Browser.svelte';
import '../../../src/app.css';

mount(Browser, { target: document.getElementById('fixture')! });
