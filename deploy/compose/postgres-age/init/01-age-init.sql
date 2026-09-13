-- 初始化 LoomVec 数据库：加载 AGE 扩展并建图占位（应用侧连接时仍需 LOAD 'age'）
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
SELECT create_graph('loomvec_graph');
