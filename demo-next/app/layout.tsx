import "./globals.css";

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <><style>{`html,body{margin:0}`}</style><main lang="zh-CN">{children}</main></>;
}
