-- 跟踪榜的 business_date 必须是“已实现净值日”，不是发布墙钟日。
-- 历史日更曾把 business_date 写成今天（nav_as_of 还是昨天/上周五），
-- 导致 first_seen_date 提前一天，并使同日修正报告因 nav_as_of 更旧被
-- tracker 拒绝（older_business_date）。这里按 publication.nav_as_of 回填。
UPDATE screening_snapshot
   SET business_date = (
       SELECT p.nav_as_of FROM publication p
        WHERE p.publication_id = screening_snapshot.publication_id
   )
 WHERE EXISTS (
       SELECT 1 FROM publication p
        WHERE p.publication_id = screening_snapshot.publication_id
          AND p.nav_as_of IS NOT NULL
          AND p.nav_as_of <> screening_snapshot.business_date
 );
