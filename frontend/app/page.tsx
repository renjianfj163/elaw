export default function HomePage() {
  return (
    <main style={{ width: "min(720px, calc(100% - 32px))", margin: "40px auto" }}>
      <p style={{ background: "#8a3b2c", color: "white", padding: "10px 14px", borderRadius: 8 }}>
        辅助预审，不构成法律意见。
      </p>
      <h1>本机工作台</h1>
      <p>请双击「启动（无需Docker）.bat」。脚本会在这台电脑上打开工作台，报告不会发到公开网页。</p>
    </main>
  );
}
