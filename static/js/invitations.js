const shareStatus = document.getElementById("invitation-share-status");

document.querySelectorAll("[data-copy-target], [data-share-target]").forEach((button) => {
    button.addEventListener("click", async () => {
        const isSharing = Boolean(button.dataset.shareTarget);
        const field = document.getElementById(button.dataset.shareTarget || button.dataset.copyTarget);
        field.focus();
        field.select();
        try {
            if (isSharing && navigator.share) {
                await navigator.share({
                    title: "Join my garden on BloomUs",
                    text: "Let's grow something lovely together. Join my private garden:",
                    url: field.value,
                });
                shareStatus.textContent = "Invitation shared. Your partner can now follow the link.";
            } else if (navigator.clipboard && navigator.clipboard.writeText) {
                await navigator.clipboard.writeText(field.value);
                shareStatus.textContent = "Copied! Paste it into a private message to your partner.";
            } else {
                shareStatus.textContent = "Copy isn't available in this browser. The text is selected; copy it manually and share it privately.";
            }
        } catch (error) {
            shareStatus.textContent = error.name === "AbortError"
                ? "Sharing cancelled. You can still copy the link."
                : "Couldn't share automatically. The text is selected; copy it manually and share it privately.";
        }
    });
});
