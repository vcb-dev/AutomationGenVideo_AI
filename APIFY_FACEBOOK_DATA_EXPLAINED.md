# 📊 APIFY FACEBOOK DATA - CHI TIẾT

## ✅ **DỮ LIỆU APIFY TRẢ VỀ**

### **1. Metadata (Tổng quan)**
```json
{
  "total_items": 10,        // ← Số items đã fetch (KHÔNG phải tổng số posts của channel)
  "posts_count": 10,        // ← Số posts đã fetch (KHÔNG phải tổng số posts)
  "followers_count": 3,     // ← Số likes trong posts (KHÔNG phải followers của channel)
  "likes_count": 3          // ← Tổng likes từ các posts
}
```

### **2. Mỗi Post/Video**
```json
{
  "postId": "1219808399646522",
  "pageName": "huyk.kimhoanvienchibao",
  "url": "https://www.facebook.com/...",
  "time": "2026-01-30T06:59:34.000Z",
  "timestamp": 1769756374,
  
  // User Info
  "user": {
    "id": "100063721444581",
    "name": "HuyK - Kim Hoàn",
    "profileUrl": "https://www.facebook.com/100063721444581",
    "profilePic": "https://..."
  },
  
  // Content
  "text": "Anh chị nhắn tin để đặt hàng nhé!",
  
  // Engagement (CHỈ CỦA BÀI VIẾT NÀY)
  "likes": 3,               // ← Likes của bài viết này
  "comments": 19,           // ← Comments của bài viết này
  "shares": 0,              // ← Shares của bài viết này
  
  // Media
  "isVideo": true,
  "media": [
    {
      "thumbnail": "https://...",
      "__typename": "Video",
      "id": "1219808399646522"
    }
  ]
}
```

---

## ❌ **APIFY KHÔNG TRẢ VỀ**

### **Channel-level Stats:**
- ❌ **Tổng số Followers** của channel
- ❌ **Tổng số Videos** của channel
- ❌ **Tổng số Posts** của channel
- ❌ **Total Views** của channel
- ❌ **Engagement Rate** của channel

### **Chỉ có:**
- ✅ **Danh sách posts** đã fetch (max 20-50 items)
- ✅ **Engagement của từng post** (likes, comments, shares)
- ✅ **User info** (name, avatar, profile URL)
- ✅ **Media** (thumbnails, video URLs)

---

## 📈 **CÓ THỂ TÍNH TOÁN**

### **1. Total Likes (từ posts đã fetch):**
```javascript
const totalLikes = posts.reduce((sum, post) => sum + (post.likes || 0), 0);
// Ví dụ: Post 1 (3 likes) + Post 2 (5 likes) = 8 total likes
```

### **2. Total Comments (từ posts đã fetch):**
```javascript
const totalComments = posts.reduce((sum, post) => sum + (post.comments || 0), 0);
```

### **3. Total Shares (từ posts đã fetch):**
```javascript
const totalShares = posts.reduce((sum, post) => sum + (post.shares || 0), 0);
```

### **4. Videos Count (từ posts đã fetch):**
```javascript
const videosCount = posts.filter(post => post.isVideo).length;
```

### **5. Images Count (từ posts đã fetch):**
```javascript
const imagesCount = posts.filter(post => !post.isVideo).length;
```

---

## ⚠️ **LƯU Ý QUAN TRỌNG**

### **1. Không phải tổng số thực:**
```
Apify fetch 20 posts → total_likes = 100
NHƯNG channel có thể có 1000 posts với 50,000 likes!
```

### **2. Chỉ là sample data:**
```
Fetched Posts: 20/1000 (2%)
Total Likes: 100/50,000 (0.2%)
```

### **3. Không có channel stats:**
```
❌ Channel Followers: N/A
❌ Channel Total Posts: N/A
❌ Channel Total Videos: N/A
✅ Fetched Posts: 20
✅ Likes from fetched posts: 100
```

---

## 🎯 **HIỂN THỊ ĐÚNG**

### **❌ SAI:**
```
Followers: 0
Videos: 20
Posts: 20
```
→ Gây hiểu lầm là channel có 20 videos/posts

### **✅ ĐÚNG:**
```
Total Likes: 2.7K
(Từ 20 bài viết đã quét)
```
→ Rõ ràng đây là data từ posts đã fetch

---

## 📊 **RESPONSE STRUCTURE**

### **Backend Response:**
```json
{
  "type": "profile",
  "method": "apify",
  "name": "HuyK - Kim Hoàn",
  "identifier": "100063721444581",
  
  // Channel Stats (NULL vì không có)
  "followers_count": null,
  "posts_count": 10,  // ← Số posts đã fetch, KHÔNG phải tổng số
  
  // Posts Data
  "posts": [...],     // 10 posts
  "videos": [...],    // 5 videos (từ 10 posts)
  "images": [...],    // 5 images (từ 10 posts)
  
  // Metadata
  "metadata": {
    "user_profile_pic": "https://...",
    "user_profile_url": "https://...",
    "note": "Followers count not available via Apify",
    "fetched_posts": 10,
    "videos_count": 5,
    "images_count": 5
  }
}
```

---

## 💡 **KẾT LUẬN**

### **Apify chỉ cung cấp:**
1. ✅ **Danh sách posts** (sample, không phải tất cả)
2. ✅ **Engagement của từng post** (likes, comments, shares)
3. ✅ **User info** (name, avatar, URL)
4. ✅ **Media** (thumbnails, videos)

### **Apify KHÔNG cung cấp:**
1. ❌ **Channel-level stats** (followers, total posts, total videos)
2. ❌ **Total engagement** của channel
3. ❌ **Historical data**
4. ❌ **Growth metrics**

### **Giải pháp:**
- Chỉ hiển thị **Total Likes** từ posts đã fetch
- Thêm note: "Từ X bài viết đã quét"
- Không hiển thị Followers, Videos, Posts count
- Chờ Graph API approval để có channel stats chính xác

---

*Last Updated: 2026-01-30 16:32*
