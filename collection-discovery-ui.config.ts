export const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

export type ApiFilterFunction = (collection: any) => boolean;

export interface ApiConfiguration {
  url: string;
  filter?: ApiFilterFunction;
  filterDescription?: string;
}

export const DEFAULT_API_CONFIGURATIONS: ApiConfiguration[] = [
  {
    url: "http://localhost:8080/",
  },
];
