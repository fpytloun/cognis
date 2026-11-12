/** Human document access and public links both follow the document's newest revision. */
export interface DocumentPersonGrant {
  principal_email: string;
  allow_comments: boolean;
  version: number;
  revoked: boolean;
}
export interface DocumentPeople {
  kind: 'artifact' | 'deliverable';
  lineage_id: string;
  items: DocumentPersonGrant[];
}
export interface DocumentPersonRequest {
  principal_email: string;
  expected_version: number;
  allow_comments: boolean;
  revoked: boolean;
}
