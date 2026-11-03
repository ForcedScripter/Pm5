import { create } from "zustand";
import { persist } from "zustand/middleware";

interface AuthState {
  token: string | null;
  username: string | null;
  setSession: (token: string, username: string) => void;
  logout: () => void;
}

export const useAuth = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      username: null,
      setSession: (token, username) => set({ token, username }),
      logout: () => set({ token: null, username: null }),
    }),
    { name: "pm5-auth" },
  ),
);
