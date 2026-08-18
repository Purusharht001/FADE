import { create } from "zustand";
import type { TokenPair, UserRead } from "@/types/api";

const STORAGE_KEY = "fade-auth";

interface StoredAuth {
  accessToken: string;
  refreshToken: string;
  user: UserRead;
}

function loadStoredAuth(): StoredAuth | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as StoredAuth) : null;
  } catch {
    return null;
  }
}

function persistAuth(auth: StoredAuth | null) {
  if (auth) localStorage.setItem(STORAGE_KEY, JSON.stringify(auth));
  else localStorage.removeItem(STORAGE_KEY);
}

interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  user: UserRead | null;
  isAuthenticated: boolean;
  setSession: (tokens: TokenPair, user: UserRead) => void;
  logout: () => void;
}

const initial = loadStoredAuth();

export const useAuthStore = create<AuthState>((set) => ({
  accessToken: initial?.accessToken ?? null,
  refreshToken: initial?.refreshToken ?? null,
  user: initial?.user ?? null,
  isAuthenticated: Boolean(initial?.accessToken),
  setSession: (tokens, user) => {
    persistAuth({ accessToken: tokens.accessToken, refreshToken: tokens.refreshToken, user });
    set({
      accessToken: tokens.accessToken,
      refreshToken: tokens.refreshToken,
      user,
      isAuthenticated: true,
    });
  },
  logout: () => {
    persistAuth(null);
    set({ accessToken: null, refreshToken: null, user: null, isAuthenticated: false });
  },
}));

/** Non-hook accessor for use outside components (the API client's 401 handler). */
export function getAccessToken(): string | null {
  return useAuthStore.getState().accessToken;
}
