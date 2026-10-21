(() => {
    const textarea = document.getElementById("id_body");
    const preview = document.getElementById("message-preview");
    const count = document.getElementById("message-count");

    if (!textarea || !preview || !count) {
        return;
    }

    const updatePreview = () => {
        const text = textarea.value.trim();
        preview.textContent = text || "Your kind words will appear here as you write.";
        count.textContent = `${textarea.value.length} / 500`;
    };

    textarea.addEventListener("input", updatePreview);
    updatePreview();
})();
