export default function HomePage() {
  return (
    <main style={{ width: "min(720px, calc(100% - 32px))", margin: "40px auto" }}>
      <p style={{ background: "#8a3b2c", color: "white", padding: "10px 14px", borderRadius: 8 }}>
        辅助预审，不构成法律意见。
      </p>
      <h1>本机工作台</h1>
      <p>上传、核验和确认在后端页面完成。打开本机的 8000 端口即可使用。</p>
      <p>
        <a href="http://127.0.0.1:8000/">进入工作台</a>
      </p>
    </main>
  );
}
