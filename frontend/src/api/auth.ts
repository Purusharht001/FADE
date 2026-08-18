import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import { useAuthStore } from "@/store/auth";
import type { TokenPair, UserLoginRequest, UserRead, UserRegisterRequest } from "@/types/api";

export const login = (payload: UserLoginRequest) =>
  api.post<TokenPair>("/auth/login", payload, { auth: false });

export const register = (payload: UserRegisterRequest) =>
  api.post<UserRead>("/auth/register", payload, { auth: false });

export const getMe = () => api.get<UserRead>("/auth/me");

export function useLogin() {
  const setSession = useAuthStore((s) => s.setSession);
  return useMutation({
    mutationFn: async (payload: UserLoginRequest) => {
      const tokens = await login(payload);
      // /auth/login doesn't return the user profile, only tokens — fetch it
      // once with the fresh token so the store always has both together.
      const user = await api.get<UserRead>("/auth/me", {
        headers: { Authorization: `Bearer ${tokens.accessToken}` },
      });
      setSession(tokens, user);
      return user;
    },
  });
}

export function useRegister() {
  return useMutation({ mutationFn: register });
}

export function useCurrentUser() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  return useQuery({
    queryKey: ["me"],
    queryFn: getMe,
    enabled: isAuthenticated,
    staleTime: 5 * 60 * 1000,
  });
}
