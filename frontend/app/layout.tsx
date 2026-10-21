export const metadata = {
  title: "环评合规预审",
  description: "辅助预审，不构成法律意见。",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body style={{ margin: 0, background: "#f3efe4", color: "#1d2822", fontFamily: "Segoe UI, Microsoft YaHei, sans-serif" }}>
        {children}
      </body>
    </html>
  );
}
