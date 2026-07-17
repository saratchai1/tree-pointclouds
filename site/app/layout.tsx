import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Moke Garden Point Cloud",
  description: "Interactive 3D point cloud with highlighted trunk and crown measurements.",
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="th">
      <body>{children}</body>
    </html>
  );
}
