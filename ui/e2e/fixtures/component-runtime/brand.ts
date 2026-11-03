import { mount } from 'svelte';
import '../../../src/app.css';
import BrandGallery from './BrandGallery.svelte';

mount(BrandGallery, { target: document.getElementById('app')! });
