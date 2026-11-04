import type {
  OfficePreviewSession,
  PersonalKnowledgeCreate,
  PersonalKnowledgeDetail,
  PersonalKnowledgeFileListResponse,
  PersonalKnowledgeFileSummary,
  PersonalKnowledgeUpdate
} from "@hermes-cloud/schemas";

export interface PersonalKnowledgeAssetApi {
  create: (payload: PersonalKnowledgeCreate) => Promise<PersonalKnowledgeDetail>;
  detail: (knowledgeBaseId: number) => Promise<PersonalKnowledgeDetail>;
  update: (
    knowledgeBaseId: number,
    payload: PersonalKnowledgeUpdate
  ) => Promise<PersonalKnowledgeDetail>;
  remove: (knowledgeBaseId: number) => Promise<void>;
  files: (knowledgeBaseId: number) => Promise<PersonalKnowledgeFileListResponse>;
  uploadFile: (
    knowledgeBaseId: number,
    file: File
  ) => Promise<PersonalKnowledgeFileSummary>;
  renameFile: (
    knowledgeBaseId: number,
    fileId: number,
    filename: string
  ) => Promise<PersonalKnowledgeFileSummary>;
  deleteFile: (knowledgeBaseId: number, fileId: number) => Promise<void>;
  fileContent: (
    knowledgeBaseId: number,
    fileId: number,
    download: boolean
  ) => Promise<Blob>;
  officePreviewSession?: (
    knowledgeBaseId: number,
    fileId: number
  ) => Promise<OfficePreviewSession>;
}
