import type { DeliverableShareOptions } from '$lib/types/api';
export type ShareLinkOptions = DeliverableShareOptions & { mode?: 'view' | 'download'; allow_comments?: boolean };
